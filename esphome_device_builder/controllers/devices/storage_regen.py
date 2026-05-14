"""Background ``--only-generate`` regeneration helpers."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from ...helpers.config_hash import read_build_info_hash
from ...helpers.subprocess import create_subprocess_exec
from ..config import get_device_metadata, set_device_metadata

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
    loop = asyncio.get_running_loop()
    config_dir = controller._db.settings.config_dir
    config_path = controller._db.settings.rel_path(configuration)

    def _read() -> tuple[float, dict[str, Any]] | None:
        # One executor hop for both reads; the work is serial on
        # disk anyway and two parallel jobs would just consume
        # two thread-pool slots for no win.
        try:
            mtime = config_path.stat().st_mtime
        except OSError:
            return None
        return mtime, get_device_metadata(config_dir, configuration)

    result = await loop.run_in_executor(None, _read)
    if result is None:
        return False
    current_mtime, md = result
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

    Combines ``stat()`` + sidecar write into a single executor
    hop and samples wall-clock inside the closure so the stamp
    captures the same instant the file's mtime was observed.
    """
    config_dir = controller._db.settings.config_dir
    config_path = controller._db.settings.rel_path(configuration)

    def _stamp() -> None:
        try:
            mtime = config_path.stat().st_mtime
        except OSError:
            return  # file vanished mid-regen; nothing to stamp.
        set_device_metadata(
            config_dir,
            configuration,
            regen_failed_mtime=mtime,
            regen_failed_at=time.time(),
        )

    await asyncio.get_running_loop().run_in_executor(None, _stamp)


async def finalize_success(controller: DevicesController, configuration: str) -> None:
    """
    Read ``config_hash`` from ``build_info.json`` and clear the failure stamp.

    Single executor hop folds the ``read_build_info_hash`` call
    and the ``set_device_metadata`` transaction together; the
    transaction writes ``expected_config_hash`` and clears
    ``regen_failed_mtime`` / ``regen_failed_at`` atomically.
    """
    config_dir = controller._db.settings.config_dir
    yaml_path = controller._db.settings.rel_path(configuration)

    def _finalize() -> str | None:
        new_hash = read_build_info_hash(yaml_path)
        kwargs: dict[str, Any] = {
            "regen_failed_mtime": 0.0,
            "regen_failed_at": 0.0,
        }
        if new_hash:
            kwargs["expected_config_hash"] = new_hash
        set_device_metadata(config_dir, configuration, **kwargs)
        return new_hash

    new_hash = await asyncio.get_running_loop().run_in_executor(None, _finalize)
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
