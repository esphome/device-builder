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
        result = await run_subprocess_capture(
            bin_path,
            "api",
            "check-update",
            timeout=_CHECK_UPDATE_TIMEOUT,
            merge_stderr=False,
        )
        if result.timed_out:
            raise CommandError(ErrorCode.INTERNAL_ERROR, "update check timed out")
        if result.returncode != 0:
            raise CommandError(
                ErrorCode.INTERNAL_ERROR,
                f"update check failed (exit {result.returncode})",
            )
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
        await create_subprocess_exec(
            bin_path,
            "api",
            "update",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            # Own session so a backend stop/restart during the install does not
            # signal the updater along with this process group.
            start_new_session=True,
        )
        _LOGGER.info("Triggered ESPHome Desktop update via %s", bin_path)
        return {"started": True}


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
