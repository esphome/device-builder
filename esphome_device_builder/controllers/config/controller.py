"""Config controller — version, serial ports, chip detection, preferences, secrets, info."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import TYPE_CHECKING, Any

from esphome.const import __version__ as esphome_version
from esphome.storage_json import StorageJSON
from esphome.util import get_serial_ports

from ...constants import __version__ as server_version
from ...helpers.api import CommandError, api_command
from ...helpers.secrets_state import (
    SecretsContentError,
    is_valid_secret_key,
    read_secrets_yaml,
    validate_wifi_credentials,
    write_secret,
    write_wifi_secrets,
)
from ...helpers.storage import ShutdownCallback
from ...helpers.storage_path import resolve_storage_path
from ...models import ErrorCode, UserPreferences
from ._preferences_store import PreferencesStore
from .chip_detect import (
    _detect_chip_via_esptool,
    _detect_failure_message,
    _is_valid_port_name,
    _read_app_descriptor_board_id,
)

if TYPE_CHECKING:
    from ...device_builder import DeviceBuilder


class ConfigController:
    """Manages application configuration, preferences, and system info."""

    def __init__(self, device_builder: DeviceBuilder) -> None:
        self._db = device_builder
        self._shutdown_callbacks: list[ShutdownCallback] = []
        self.prefs = PreferencesStore(
            device_builder.settings.config_dir, self._shutdown_callbacks.append
        )

    async def async_load(self) -> None:
        """Seed the RAM-canonical preferences store (and migrate on first run)."""
        await self.prefs.async_load()

    async def stop(self) -> None:
        """Flush the preferences store on shutdown."""
        for callback in self._shutdown_callbacks:
            await callback()

    @api_command("config/version")
    async def get_version(self, **kwargs: Any) -> dict:
        """Get ESPHome and server version."""
        return {"server_version": server_version, "esphome_version": esphome_version}

    @api_command("config/serial_ports")
    async def get_serial_ports_cmd(self, **kwargs: Any) -> list[dict]:
        """List available serial ports."""
        loop = asyncio.get_running_loop()
        ports = await loop.run_in_executor(None, get_serial_ports)
        return [
            {"port": p.path, "desc": p.description if p.description != "n/a" else p.path}
            for p in ports
        ]

    @api_command("config/detect_chip")
    async def detect_chip_cmd(self, **kwargs: Any) -> dict:
        """Identify what's plugged into a server-side serial port.

        Runs ``esptool chip-id`` to detect the chip family, then
        best-effort reads the IDF ``esp_app_desc_t`` at flash
        offset ``0x10020`` for the ``project_name`` field (the
        board_id baked in by ESPHome at compile time when factory
        firmware is present). Closes the parity gap with WebSerial,
        which already does the same locally via esptool-js.

        Returns ``{chip_family, variant, platform, board_id?}``.
        ``board_id`` is omitted whenever the manifest read fails or
        the device isn't running an IDF image — callers treat that
        as "narrow the picker by chip family" and let the user pick
        the specific board.

        Failures all surface as ``UNAVAILABLE`` but with distinct
        messages so the user can act: "port busy" (close the
        offending app), "no response" (check the cable / BOOT
        button), "unknown chip" (the device responded but isn't
        an ESP variant we recognise), etc.
        """
        port = kwargs.get("port")
        if not isinstance(port, str) or not port:
            raise CommandError(ErrorCode.INVALID_ARGS, "port is required")
        if not _is_valid_port_name(port):
            raise CommandError(ErrorCode.INVALID_ARGS, f"invalid port: {port!r}")

        chip_info, failure = await _detect_chip_via_esptool(port)
        if chip_info is None:
            raise CommandError(ErrorCode.UNAVAILABLE, _detect_failure_message(failure, port))

        result: dict = dict(chip_info)
        board_id = await _read_app_descriptor_board_id(port)
        if board_id:
            result["board_id"] = board_id
        return result

    @api_command("config/get_preferences")
    async def get_prefs(self, **kwargs: Any) -> UserPreferences:
        """Get user preferences (RAM-canonical via the store)."""
        return self.prefs.snapshot()

    @api_command("config/set_preferences")
    async def set_prefs(self, **kwargs: Any) -> UserPreferences:
        """Update user preferences.

        Accepts partial updates — only provided fields are changed,
        others keep their current values.
        """
        update_fields = {k: v for k, v in kwargs.items() if k not in ("client", "message_id")}
        version_history = self._db.version_history
        if "version_history_enabled" in update_fields and version_history is not None:
            # Validate the batch and decode the flag without persisting, then
            # reconcile the watcher *before* the write lands. A bad field or a
            # failed reconcile raises with the store untouched — no rollback that
            # could clobber a concurrent write to another field.
            candidate = self.prefs.merged(update_fields)
            await version_history.set_auto_commit(enabled=candidate.version_history_enabled)
        return self.prefs.update(update_fields)

    @api_command("config/get_secrets")
    async def get_secrets(self, **kwargs: Any) -> list[str]:
        """Get secret key names from secrets.yaml."""
        loop = asyncio.get_running_loop()
        config_dir = self._db.settings.config_dir
        data = await loop.run_in_executor(None, read_secrets_yaml, config_dir)
        if not data:
            return []
        # ``secrets.yaml`` could legitimately have non-string keys
        # (a YAML scalar like ``42:`` parses to ``int``). ``sorted()``
        # on mixed types raises ``TypeError`` in Python 3, so filter
        # to string keys before sorting — non-string keys aren't
        # usable in ``!secret`` references anyway.
        return sorted(k for k in data if isinstance(k, str))

    @api_command("config/set_secret")
    async def set_secret(
        self, *, key: str, value: str, overwrite: bool = True, **kwargs: Any
    ) -> dict:
        """
        Atomically set one key in ``secrets.yaml``; return ``{created}``.

        The read-modify-write runs under the shared secrets write lock so
        concurrent secrets.yaml mutations don't clobber each other.
        ``overwrite=False`` leaves an existing key untouched (create-if-absent).
        """
        if not is_valid_secret_key(key):
            raise CommandError(ErrorCode.INVALID_ARGS, "invalid secret key")
        if not isinstance(value, str):
            raise CommandError(ErrorCode.INVALID_ARGS, "value must be a string")
        if not isinstance(overwrite, bool):
            raise CommandError(ErrorCode.INVALID_ARGS, "overwrite must be a boolean")
        config_dir = self._db.settings.config_dir
        try:
            # Funnel through DeviceBuilder so the editor's now-stale !secret
            # lint is dropped along with the write (see write_secrets_locked).
            created = await self._db.write_secrets_locked(
                partial(write_secret, config_dir, key, value, overwrite=overwrite),
            )
        except SecretsContentError as err:
            raise CommandError(
                ErrorCode.INVALID_ARGS, f"refusing to save invalid secrets.yaml: {err}"
            ) from err
        return {"created": created}

    @api_command("config/set_wifi_credentials")
    async def set_wifi_credentials(
        self,
        *,
        ssid: str,
        password: str = "",
        **kwargs: Any,
    ) -> dict:
        """
        Set ``wifi_ssid`` / ``wifi_password`` in ``secrets.yaml``.

        Backs the kebab "Set up Wi-Fi" action; the create wizard's own
        Wi-Fi entry is persisted by ``devices/create``. Validates inputs
        (shared with that path) so a malformed value can't slip through to
        the next ``compile``, and preserves any other secret keys + the
        file's comments via a line-based rewrite.
        """
        try:
            validate_wifi_credentials(ssid, password)
        except SecretsContentError as err:
            raise CommandError(ErrorCode.INVALID_ARGS, str(err)) from err
        config_dir = self._db.settings.config_dir
        await self._db.write_secrets_locked(write_wifi_secrets, config_dir, ssid, password)
        return {}

    @api_command("config/get_info")
    async def get_info(self, *, configuration: str, **kwargs: Any) -> dict | None:
        """Get compiled device metadata (StorageJSON) for a configuration."""
        loop = asyncio.get_running_loop()

        def _load_info() -> dict | None:
            # ``rel_path`` calls ``Path.resolve`` (an ``os.path.abspath``
            # syscall under the hood) and the StorageJSON load below
            # opens the sidecar from disk — both block the event loop
            # if run inline. Do them together inside the executor so
            # a slow filesystem (NFS-mounted config dir, EBS-backed
            # Docker volume) can't stall the dashboard. ``rel_path``
            # raises ``CommandError`` on traversal; the awaited future
            # propagates that out to the WS dispatcher unchanged.
            self._db.settings.rel_path(configuration)
            storage = StorageJSON.load(resolve_storage_path(configuration))
            if storage is None:
                return None
            return {
                "name": storage.name,
                "friendly_name": storage.friendly_name,
                "comment": storage.comment,
                "address": storage.address,
                "web_port": storage.web_port,
                "target_platform": storage.target_platform,
                "current_version": storage.esphome_version,
                "deployed_version": storage.firmware_bin_path,
                "loaded_integrations": storage.loaded_integrations,
            }

        return await loop.run_in_executor(None, _load_info)
