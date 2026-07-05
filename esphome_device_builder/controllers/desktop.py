"""ESPHome Desktop update integration.

When the dashboard runs inside the ESPHome Desktop app (0.14.0+), the app
exports ``ESPHOME_DESKTOP_BIN`` pointing at its ``esphome-desktop`` CLI, which
speaks a stable, versioned JSON ``api`` over stdout (NDJSON). These commands let
the frontend check for and trigger a full desktop-app update (the desktop app,
ESPHome, and the device builder) from the dashboard's kebab menu.

The update is fire-and-forget: ``esphome-desktop api update`` stops and restarts
this backend to install, so the WS connection drops mid-update; the frontend
re-checks after it reconnects. The desktop app completes the update regardless
of this process, so the updater is spawned detached (its own session) and not
awaited.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from ..helpers.api import CommandError, api_command
from ..helpers.json import loads
from ..helpers.subprocess import create_subprocess_exec, run_subprocess_capture
from ..models import ErrorCode

if TYPE_CHECKING:
    from esphome_device_builder.device_builder import DeviceBuilder

_LOGGER = logging.getLogger(__name__)

# `api check-update` spawns Python for the installed versions and hits GitHub
# and PyPI; give it headroom over a local command but keep it bounded.
_CHECK_UPDATE_TIMEOUT = 120.0


class DesktopController:
    """WebSocket endpoints bridging to the ESPHome Desktop CLI (0.14.0+)."""

    def __init__(self, db: DeviceBuilder) -> None:
        self._db = db

    def _desktop_bin(self) -> str:
        """Return the CLI path, or raise if the desktop app isn't update-capable.

        The frontend only shows these actions when ``desktop_update_capable``
        is set, so reaching here without a binary means a stale/forged call;
        fail loudly rather than shelling out to nothing.
        """
        bin_path = self._db.settings.desktop_bin
        if not bin_path:
            raise CommandError(
                ErrorCode.INTERNAL_ERROR,
                "not running under an update-capable ESPHome Desktop app",
            )
        return bin_path

    @api_command("desktop/check_update")
    async def check_update(self, **kwargs: Any) -> dict[str, Any]:
        """Report whether any component has an update available (read-only).

        Shells out to ``esphome-desktop api check-update`` and returns its
        parsed JSON: ``{any_available, app, esphome, device_builder}`` where
        each component carries ``available``, ``installed``, ``latest``, and
        ``error``.
        """
        bin_path = self._desktop_bin()
        try:
            result = await run_subprocess_capture(
                bin_path,
                "api",
                "check-update",
                timeout=_CHECK_UPDATE_TIMEOUT,
                merge_stderr=False,
            )
        except OSError as err:
            # Stale/removed ESPHOME_DESKTOP_BIN, permissions, etc. Surface a
            # clean error instead of an INTERNAL_ERROR traceback.
            _LOGGER.warning("Could not run ESPHome Desktop CLI %s: %s", bin_path, err)
            raise CommandError(
                ErrorCode.INTERNAL_ERROR, "could not run the ESPHome Desktop CLI"
            ) from err
        if result.timed_out:
            _LOGGER.warning("ESPHome Desktop update check timed out")
            raise CommandError(ErrorCode.INTERNAL_ERROR, "update check timed out")
        if result.returncode != 0:
            # The CLI prints its errors as a JSON `{"type":"err",...}` line on
            # stdout (kept clean by merge_stderr=False), so surface its message.
            detail = _error_detail(result.stdout)
            _LOGGER.warning(
                "ESPHome Desktop update check failed (exit %s): %s",
                result.returncode,
                detail,
            )
            raise CommandError(ErrorCode.INTERNAL_ERROR, f"update check failed: {detail}")
        try:
            # orjson-backed helper; JSONDecodeError (and _last_json_line's
            # "no output") both subclass ValueError.
            payload = loads(_last_json_line(result.stdout))
        except ValueError as err:
            raise CommandError(
                ErrorCode.INTERNAL_ERROR, "could not parse update check output"
            ) from err
        if not isinstance(payload, dict):
            raise CommandError(ErrorCode.INTERNAL_ERROR, "unexpected update check output")
        return payload

    @api_command("desktop/update")
    async def update(self, **kwargs: Any) -> dict[str, Any]:
        """Trigger the full desktop update, fire-and-forget.

        Spawns ``esphome-desktop api update`` detached and returns immediately.
        Stopping this backend for the install won't kill the detached updater,
        and the desktop app finishes the update even if it did; the frontend
        polls ``desktop/check_update`` again after the WS reconnects.
        """
        bin_path = self._desktop_bin()
        try:
            await create_subprocess_exec(
                bin_path,
                "api",
                "update",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                # Own session so a backend stop/restart during the install does
                # not signal the updater along with this process group.
                start_new_session=True,
            )
        except OSError as err:
            _LOGGER.warning("Could not run ESPHome Desktop CLI %s: %s", bin_path, err)
            raise CommandError(
                ErrorCode.INTERNAL_ERROR, "could not run the ESPHome Desktop CLI"
            ) from err
        _LOGGER.info("Triggered ESPHome Desktop update via %s", bin_path)
        return {"started": True}


def _error_detail(stdout: bytes) -> str:
    """Human-readable failure detail from the CLI's error line, if present.

    On a non-zero exit the `api` prints a JSON `{"type":"err",...,"message":...}`
    line on stdout; extract that message so operators and the client see the
    real reason rather than a bare exit code.
    """
    try:
        payload = loads(_last_json_line(stdout))
    except ValueError:
        return "no diagnostic output"
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, str):
            return message
    return "unrecognized error output"


def _last_json_line(stdout: bytes) -> str:
    """Last non-empty line of *stdout*, decoded.

    ``api check-update`` emits exactly one JSON line, but taking the last
    non-empty line is robust to any trailing newline or stray leading output.
    """
    text = stdout.decode("utf-8", "replace")
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped
    raise ValueError("no output")
