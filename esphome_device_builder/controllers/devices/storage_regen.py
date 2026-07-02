"""Background ``--only-generate`` regeneration helpers."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from ...constants import is_secrets_file
from ...helpers.async_ import run_in_executor
from ...helpers.config_hash import read_build_info_hash
from ...helpers.subprocess import create_subprocess_exec

if TYPE_CHECKING:
    from .controller import DevicesController

_LOGGER = logging.getLogger(__name__)

# How long the persisted "regen failed" stamp is honoured before
# a restart-time check is allowed to re-spawn ``--only-generate``
# for the same untouched YAML. One hour: short enough that a
# debugger restart-loop doesn't churn through 10 spawns on the
# same broken config, long enough that the user can come back
# later without having to touch the file.
_REGEN_FAILURE_TTL_SECONDS: float = 3600.0


def schedule(controller: DevicesController, configuration: str) -> None:
    """
    Run ``esphome compile --only-generate <yaml>`` in the background.

    Three guards bound the spawn rate: in-memory pending +
    failed sets (per-session), an on-disk failure stamp
    (cross-restart, TTL-gated), and ``_regenerate_lock``
    serialising the subprocess itself.
    """
    if is_secrets_file(configuration):
        # Shared credentials, not a buildable config: no build dir to
        # --only-generate, so a regen would only warn about a missing hash.
        return
    if not controller.state.esphome_cmd:
        return  # ``start()`` hasn't run yet.
    if configuration in controller.state.regenerate_pending:
        return  # already scheduled.
    if configuration in controller.state.regenerate_failed:
        # Same-session retry would replay the same error.
        return

    # Mark synchronously so a second same-tick call sees the
    # marker before the coroutine yields. ``_run``'s finally
    # discards on completion.
    controller.state.regenerate_pending.add(configuration)
    controller._db.create_background_task(_run(controller, configuration))


async def _run(controller: DevicesController, configuration: str) -> None:
    try:
        # Routed through the controller's bound delegates so
        # tests patching any of the four async helpers on the
        # class still intercept.
        if await controller._regen_already_failed_recently_async(configuration):
            controller.state.regenerate_failed.add(configuration)
            return
        async with controller._regenerate_lock:
            success = await controller._spawn_only_generate(configuration)
        if success:
            await controller._finalize_regen_success(configuration)
            await controller._scanner.reload(configuration)
        else:
            controller.state.regenerate_failed.add(configuration)
            await controller._stamp_regen_failure(configuration)
    finally:
        controller.state.regenerate_pending.discard(configuration)


async def spawn_only_generate(controller: DevicesController, configuration: str) -> bool:
    """
    Run ``esphome compile --only-generate`` once. Return True iff exit code 0.

    Exceptions during spawn and non-zero exit codes both
    produce False so the caller takes the same
    persist-failure-stamp branch.
    """
    config_path = str(controller._db.settings.rel_path(configuration))
    cmd = [*controller.state.esphome_cmd, "--dashboard", "compile", "--only-generate", config_path]
    try:
        proc = await create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
    except Exception:
        _LOGGER.debug("Storage regenerate spawn failed for %s", configuration, exc_info=True)
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


async def already_failed_recently_async(controller: DevicesController, configuration: str) -> bool:
    """
    Return True iff the persisted failure stamp is unchanged-and-fresh.

    Both halves must hold: the YAML's ``stat.st_mtime`` equals
    the cached ``regen_failed_mtime``, and the cached
    ``regen_failed_at`` is within ``_REGEN_FAILURE_TTL_SECONDS``
    (clamped against future-dated stamps so clock skew can't
    lock the regen out indefinitely).
    """
    config_path = controller._db.settings.rel_path(configuration)

    try:
        current_mtime = await run_in_executor(lambda: config_path.stat().st_mtime)
    except OSError:
        return False
    md = controller._metadata_store.get(configuration)
    cached_mtime = md.get("regen_failed_mtime")
    cached_at = md.get("regen_failed_at")
    if not cached_mtime or not cached_at:
        return False
    try:
        mtime_matches = float(cached_mtime) == current_mtime
        age = max(0.0, time.time() - float(cached_at))
    except (TypeError, ValueError):
        return False
    return mtime_matches and age < _REGEN_FAILURE_TTL_SECONDS


async def stamp_failure(controller: DevicesController, configuration: str) -> None:
    """
    Persist the cross-restart "we already tried, gave up" marker.

    Reads the YAML's mtime on the executor and samples
    wall-clock alongside it so the stamp captures the same
    instant the file's mtime was observed.
    """
    config_path = controller._db.settings.rel_path(configuration)
    try:
        mtime = await run_in_executor(lambda: config_path.stat().st_mtime)
    except OSError:
        return  # file vanished mid-regen; nothing to stamp.
    controller._metadata_store.update(
        configuration,
        regen_failed_mtime=mtime,
        regen_failed_at=time.time(),
    )


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
