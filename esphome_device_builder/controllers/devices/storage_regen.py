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
from ...helpers.subprocess import run_subprocess_capture

if TYPE_CHECKING:
    from .controller import DevicesController

_LOGGER = logging.getLogger(__name__)

# How long a failure stamp is honoured before the same untouched YAML
# earns a fresh attempt budget.
_REGEN_FAILURE_TTL_SECONDS: float = 4 * 3600.0

# A transient environment failure (interrupted clone, DNS) is
# indistinguishable from a broken YAML without parsing stderr, so every
# failure earns a bounded escalating retry; the attempt count persists
# in the metadata store, resuming the ladder across restarts.
_RETRY_BACKOFF_BASE_SECONDS: float = 30.0
_MAX_REGEN_ATTEMPTS: int = 4

# A hung run (stalled package fetch) would hold ``_regenerate_lock``
# indefinitely; matches the config-bundle build timeout.
_ONLY_GENERATE_TIMEOUT_SECONDS: float = 300.0


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
        regen = controller.state.regen
        try:
            current_mtime = (await run_in_executor(config_path.stat)).st_mtime
        except FileNotFoundError:
            _LOGGER.debug("Storage regenerate for %s: config vanished; skipping", configuration)
            regen.clear_strikes(configuration)
            return
        except OSError:
            # A transient errno (EACCES, EIO on a network mount) must not
            # strand the ladder: the firing timer already popped itself.
            # Without an mtime the stamp can't key the failure, so a RAM
            # strike escalates the re-arm up to the TTL.
            strikes = regen.record_strike(configuration)
            _LOGGER.warning(
                "Storage regenerate for %s: config unreadable; retrying",
                configuration,
                exc_info=True,
            )
            _arm_retry(
                controller, configuration, min(_delay_for(strikes), _REGEN_FAILURE_TTL_SECONDS)
            )
            return
        regen.clear_strikes(configuration)
        stamp = _fresh_stamp(
            controller._metadata_store.get(configuration), current_mtime, time.time()
        )
        if stamp is not None:
            attempts, age = stamp
            if attempts >= _MAX_REGEN_ATTEMPTS:
                # The give-up warning fired in the session that spent the
                # budget; without this one a restarted dashboard shows a
                # hashless device with nothing in the log to explain it.
                _LOGGER.warning(
                    "Storage regenerate for %s failed %d times previously; "
                    "retrying after the YAML changes or the failure stamp expires",
                    configuration,
                    attempts,
                )
                _arm_ttl_recheck(controller, configuration, age)
                return
            remaining = _delay_for(attempts) - age
            if remaining > 0:
                # A restart mid-window resumes the remainder, not an early spawn.
                _arm_retry(controller, configuration, remaining)
                return
        # Delegates so tests patching the class intercept.
        async with controller._regenerate_lock:
            failure = await controller._spawn_only_generate(configuration)
        if failure is None:
            regen.cancel_retry(configuration)
            await controller._finalize_regen_success(configuration)
            await controller._scanner.reload(configuration)
            return
        # Stamp against the pre-spawn mtime: an edit landing mid-run must
        # not have the old content's failure charged to the new file.
        recorded = controller._stamp_regen_failure(configuration, current_mtime)
        if recorded < _MAX_REGEN_ATTEMPTS:
            _arm_retry(controller, configuration, _delay_for(recorded))
            return
        _LOGGER.warning(
            "Storage regenerate for %s failed %d times (%s); retrying after "
            "the YAML changes or the failure stamp expires",
            configuration,
            recorded,
            failure,
        )
        _arm_ttl_recheck(controller, configuration)
    finally:
        controller.state.regen.pending.discard(configuration)


async def spawn_only_generate(controller: DevicesController, configuration: str) -> str | None:
    """
    Run ``esphome compile --only-generate`` once.

    Returns None on success, else a short failure summary the
    give-up warning can carry.
    """
    config_path = str(controller._db.settings.rel_path(configuration))
    cmd = [*controller.state.esphome_cmd, "--dashboard", "compile", "--only-generate", config_path]
    try:
        # Merged streams (the helper's default): esphome safe_prints
        # validation errors to stdout while load errors log to stderr;
        # the summary needs both.
        result = await run_subprocess_capture(*cmd, timeout=_ONLY_GENERATE_TIMEOUT_SECONDS)
    except Exception as err:
        _LOGGER.debug("Storage regenerate failed for %s", configuration, exc_info=True)
        return f"subprocess error: {err!r}"
    text = result.stdout[-4096:].decode(errors="replace").strip()
    if result.timed_out:
        _LOGGER.warning(
            "Storage regenerate for %s timed out after %.0fs; output tail: %s",
            configuration,
            _ONLY_GENERATE_TIMEOUT_SECONDS,
            text[-500:],
        )
        return f"timed out after {_ONLY_GENERATE_TIMEOUT_SECONDS:.0f}s"
    if result.returncode == 0:
        return None
    _LOGGER.debug(
        "Storage regenerate for %s exited %s: %s",
        configuration,
        result.returncode,
        text[-500:],
    )
    summary = f"exit {result.returncode}"
    return f"{summary}: {text[-200:]}" if text else summary


def stamp_failure(controller: DevicesController, configuration: str, mtime: float) -> int:
    """
    Record one more failed attempt against *mtime*.

    Returns the attempt count now on record. An edit or TTL expiry
    since the prior failure resets the count to 1.
    """
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

    A missing or unparseable attempt count reads as terminal;
    future-dated stamps clamp to age 0.
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


def _arm_ttl_recheck(controller: DevicesController, configuration: str, age: float = 0.0) -> None:
    """Arm the stamp-expiry re-check unless one is already pending."""
    # No scan event fires for an untouched file, and the deadline never
    # moves for the same stamp — re-arming would only churn the timer.
    if configuration in controller.state.regen.retry_timers:
        return
    _arm_retry(controller, configuration, _REGEN_FAILURE_TTL_SECONDS - age)


def _arm_retry(controller: DevicesController, configuration: str, delay: float) -> None:
    _LOGGER.debug("Storage regenerate retry for %s in %.0fs", configuration, delay)
    loop = asyncio.get_running_loop()
    controller.state.regen.arm_retry(
        configuration, loop.call_later(delay, _fire_retry, controller, configuration)
    )


def _fire_retry(controller: DevicesController, configuration: str) -> None:
    controller.state.regen.retry_timers.pop(configuration, None)
    schedule(controller, configuration)
