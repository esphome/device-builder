"""Background ``--only-generate`` regeneration helpers."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ...constants import is_secrets_file
from ...helpers.async_ import run_in_executor
from ...helpers.build_size import coerce_sidecar_int
from ...helpers.config_hash import read_build_info_hash
from ...helpers.subprocess import create_subprocess_exec, kill_quietly

if TYPE_CHECKING:
    from .controller import DevicesController

_LOGGER = logging.getLogger(__name__)

# How long a failure stamp is honoured before the same untouched YAML
# earns a fresh attempt budget. Four hours: transient problems are the
# in-window retry ladder's job, so the TTL only paces re-checks (and
# the give-up warning) for a genuinely broken config; an edit resets
# the ladder immediately either way.
_REGEN_FAILURE_TTL_SECONDS: float = 4 * 3600.0

# Every failure gets a bounded escalating retry before the terminal
# stamp: a transient environment failure (interrupted package clone,
# DNS, network) is indistinguishable from a broken YAML without parsing
# stderr, and parsing is brittle. The attempt count persists in the
# metadata store, so the ladder resumes across restarts.
_RETRY_BACKOFF_BASE_SECONDS: float = 30.0
_MAX_REGEN_ATTEMPTS: int = 4


def schedule(controller: DevicesController, configuration: str) -> None:
    """
    Run ``esphome compile --only-generate <yaml>`` in the background.

    Spawn rate is bounded three ways: the in-memory pending set, the
    persisted failure stamp ``_run`` consults (attempt budget, backoff
    min-age, TTL), and ``_regenerate_lock`` serialising the subprocess.
    """
    if is_secrets_file(configuration):
        # Shared credentials, not a buildable config: no build dir to
        # --only-generate, so a regen would only warn about a missing hash.
        return
    if not controller.state.esphome_cmd:
        return  # ``start()`` hasn't run yet.
    if configuration in controller.state.regen.pending:
        return  # already scheduled.

    # Mark synchronously so a second same-tick call sees the
    # marker before the coroutine yields. ``_run``'s finally
    # discards on completion.
    controller.state.regen.pending.add(configuration)
    controller._db.create_background_task(_run(controller, configuration))


async def _run(controller: DevicesController, configuration: str) -> None:
    try:
        config_path = controller._db.settings.rel_path(configuration)
        try:
            current_mtime = await run_in_executor(lambda: config_path.stat().st_mtime)
        except OSError:
            # Unreadable or vanished config; a spawn would fail against it.
            _LOGGER.debug("Storage regenerate for %s: config unreadable; skipping", configuration)
            return
        stamp = _fresh_stamp(
            controller._metadata_store.get(configuration), current_mtime, time.time()
        )
        if stamp is not None:
            attempts, age = stamp
            if attempts >= _MAX_REGEN_ATTEMPTS:
                _LOGGER.debug(
                    "Storage regenerate for %s spent its attempt budget; "
                    "waiting for a YAML change or the stamp TTL",
                    configuration,
                )
                return
            remaining = _delay_for(attempts) - age
            if remaining > 0:
                # Resume an interrupted backoff (a restart mid-window)
                # with the remainder rather than spawning early.
                _arm_retry(controller, configuration, remaining)
                return
        # The spawn/stamp/finalize steps route through the controller's
        # bound delegates so tests patching them on the class intercept.
        async with controller._regenerate_lock:
            success = await controller._spawn_only_generate(configuration)
        if success:
            controller.state.regen.cancel_retry(configuration)
            await controller._finalize_regen_success(configuration)
            await controller._scanner.reload(configuration)
            return
        recorded = await controller._stamp_regen_failure(configuration)
        if recorded is None:
            _LOGGER.debug(
                "Storage regenerate for %s: config vanished mid-run; nothing recorded",
                configuration,
            )
            return
        if recorded < _MAX_REGEN_ATTEMPTS:
            _arm_retry(controller, configuration, _delay_for(recorded))
            return
        _LOGGER.warning(
            "Storage regenerate for %s failed %d times; retrying after the "
            "YAML changes or the failure stamp expires",
            configuration,
            recorded,
        )
    finally:
        controller.state.regen.pending.discard(configuration)


async def spawn_only_generate(controller: DevicesController, configuration: str) -> bool:
    """
    Run ``esphome compile --only-generate`` once. Return True iff exit code 0.

    Exceptions during spawn and non-zero exit codes both
    produce False so the caller takes the same
    retry-then-stamp branch.
    """
    config_path = str(controller._db.settings.rel_path(configuration))
    cmd = [*controller.state.esphome_cmd, "--dashboard", "compile", "--only-generate", config_path]
    try:
        proc = await create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception:
        _LOGGER.debug("Storage regenerate spawn failed for %s", configuration, exc_info=True)
        return False
    try:
        _, stderr = await proc.communicate()
    except asyncio.CancelledError:
        # The shutdown drain cancels the run mid-subprocess; don't
        # orphan the esphome/git child.
        kill_quietly(proc)
        raise
    except Exception:
        kill_quietly(proc)
        _LOGGER.debug("Storage regenerate failed for %s", configuration, exc_info=True)
        return False
    if proc.returncode != 0:
        _LOGGER.debug(
            "Storage regenerate for %s exited %s: %s",
            configuration,
            proc.returncode,
            stderr.decode(errors="replace").strip()[:500],
        )
        return False
    return True


async def stamp_failure(controller: DevicesController, configuration: str) -> int | None:
    """
    Record one more failed attempt against the YAML's current mtime.

    Returns the attempt count now on record. An edit or TTL expiry
    since the prior failure resets the count to 1; a vanished file
    stamps nothing and returns None.
    """
    config_path = controller._db.settings.rel_path(configuration)
    try:
        mtime = await run_in_executor(lambda: config_path.stat().st_mtime)
    except OSError:
        return None
    now = time.time()
    prior = _fresh_stamp(controller._metadata_store.get(configuration), mtime, now)
    attempts = (prior[0] if prior is not None else 0) + 1
    controller._metadata_store.update(
        configuration,
        regen_failed_mtime=mtime,
        regen_failed_at=now,
        regen_failed_attempts=attempts,
    )
    return attempts


async def finalize_success(controller: DevicesController, configuration: str) -> None:
    """
    Read ``config_hash`` from ``build_info.json`` and clear the failure stamp.

    ``read_build_info_hash`` is blocking — runs on the
    executor; the store merge afterwards is in-RAM with a
    debounced disk write.
    """
    yaml_path = controller._db.settings.rel_path(configuration)
    new_hash = await run_in_executor(read_build_info_hash, yaml_path)
    fields: dict[str, Any] = {
        "regen_failed_mtime": 0.0,
        "regen_failed_at": 0.0,
        "regen_failed_attempts": 0,
    }
    if new_hash:
        fields["expected_config_hash"] = new_hash
    controller._metadata_store.update(configuration, **fields)
    if not new_hash:
        _LOGGER.warning(
            "Could not read config_hash from build_info.json for %s; "
            "the displayed local config hash may stay stale until the "
            "next flash. If this persists, verify build_info.json is "
            "present in the build dir and that ESPHome's schema "
            "hasn't changed.",
            configuration,
        )
        return
    _LOGGER.debug("Stored expected_config_hash for %s: %s", configuration, new_hash)


def _fresh_stamp(
    md: Mapping[str, Any], current_mtime: float, now: float
) -> tuple[int, float] | None:
    """
    Return ``(attempts, age)`` for an unexpired stamp matching *current_mtime*.

    A stamp without a parseable attempt count reads as terminal (a
    pre-attempts stamp was only written once the budget was spent).
    Future-dated stamps clamp to age 0 so clock skew can't lock the
    regen out indefinitely.
    """
    cached_mtime = md.get("regen_failed_mtime")
    cached_at = md.get("regen_failed_at")
    if not cached_mtime or not cached_at:
        return None
    try:
        if float(cached_mtime) != current_mtime:
            return None
        age = max(0.0, now - float(cached_at))
    except (TypeError, ValueError):
        return None
    if age >= _REGEN_FAILURE_TTL_SECONDS:
        return None
    attempts = coerce_sidecar_int(md.get("regen_failed_attempts"))
    # Missing, unparseable, or non-positive reads as terminal.
    return (attempts if attempts >= 1 else _MAX_REGEN_ATTEMPTS), age


def _delay_for(attempts: int) -> float:
    """Backoff delay after failed attempt number *attempts* (1-based)."""
    return _RETRY_BACKOFF_BASE_SECONDS * 2.0 ** (attempts - 1)


def _arm_retry(controller: DevicesController, configuration: str, delay: float) -> None:
    _LOGGER.debug("Storage regenerate retry for %s in %.0fs", configuration, delay)
    loop = asyncio.get_running_loop()
    controller.state.regen.arm_retry(
        configuration, loop.call_later(delay, _fire_retry, controller, configuration)
    )


def _fire_retry(controller: DevicesController, configuration: str) -> None:
    controller.state.regen.retry_timers.pop(configuration, None)
    schedule(controller, configuration)
