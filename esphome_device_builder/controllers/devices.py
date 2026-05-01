"""Devices controller — device CRUD, file watching, CLI operations, state management."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from esphome import const
from esphome.dashboard.util.text import friendly_name_slugify
from esphome.helpers import sort_ip_addresses
from esphome.storage_json import StorageJSON, ext_storage_path, ignored_devices_storage_path

try:
    from esphome.config_helpers import import_config
except ImportError:
    import_config = None  # type: ignore[assignment]

import contextlib

from ..helpers.api import api_command
from ..helpers.config_hash import compute_yaml_config_hash
from ..helpers.device_yaml import (
    generate_device_yaml,
    parse_platform_from_yaml,
)
from ..helpers.yaml import merge_component_yaml, rewrite_esphome_name
from ..models import (
    AddComponentResponse,
    AdoptableDevice,
    Device,
    DevicesResponse,
    DeviceState,
    EventType,
    JobStatus,
    JobType,
    UpdateDeviceResponse,
    WizardResponse,
)
from ._device_mqtt_coordinator import DeviceMqttCoordinator
from ._device_scanner import DeviceFileMetadata, DeviceScanner, ScanChange
from ._device_state_monitor import DeviceStateMonitor
from .config import (
    get_device_metadata,
    remove_device_metadata,
    set_device_metadata,
)

if TYPE_CHECKING:
    from ..device_builder import DeviceBuilder

_LOGGER = logging.getLogger(__name__)


class DevicesController:
    """Manage device configurations, file watching, and CLI operations."""

    def __init__(self, device_builder: DeviceBuilder) -> None:
        self._db = device_builder
        self._esphome_cmd: list[str] = []
        # Unsubscribe handle for the firmware-job-completion listener
        # wired up in start(); held so stop() can detach cleanly.
        self._unsub_job_completed: Any = None

        # Discovery / import state
        self.import_result: dict[str, Any] = {}
        self.ignored_devices: set[str] = set()

        self._scanner = DeviceScanner(
            config_dir=self._db.settings.config_dir,
            get_metadata=self._resolve_device_metadata,
            on_change=self._on_scan_change,
        )
        self._state_monitor = DeviceStateMonitor(
            get_devices=self._get_devices,
            on_state_change=self._on_state_change,
            on_ip_change=self._on_ip_change,
            on_version_change=self._on_version_change,
            on_config_hash_change=self._on_config_hash_change,
        )
        # MQTT routes its observations through the same state monitor so
        # source-priority is enforced in one place.
        self._mqtt_coordinator = DeviceMqttCoordinator(
            config_dir=self._db.settings.config_dir,
            get_devices=self._get_devices,
            on_state_change=lambda n, s: self._state_monitor.apply(n, s, "mqtt"),
            on_ip_change=self._state_monitor.apply_ip,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialise — load state, scan files, start mDNS + ping + MQTT discovery."""
        from .firmware import _find_esphome_cmd

        self._esphome_cmd = _find_esphome_cmd()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._load_ignored_devices)
        await self._scanner.scan()
        _LOGGER.info("Devices controller started — %d devices loaded", len(self._scanner.devices))
        await self._state_monitor.start()
        await self._mqtt_coordinator.reconcile()
        self._unsub_job_completed = self._db.bus.add_listener(
            EventType.JOB_COMPLETED, self._on_firmware_job_completed
        )

    async def stop(self) -> None:
        """Stop background monitors so the process exits cleanly."""
        if self._unsub_job_completed is not None:
            self._unsub_job_completed()
            self._unsub_job_completed = None
        await self._mqtt_coordinator.stop()
        await self._state_monitor.stop()

    async def poll(self) -> None:
        """Poll for file changes."""
        await self._scanner.scan()
        await self._mqtt_coordinator.reconcile()

    def get_devices(self) -> list[Device]:
        """Snapshot of the currently-loaded devices."""
        return self._scanner.devices

    def get_address_cache_args(self, configuration: str) -> list[str]:
        """
        Return ``--mdns/--dns-address-cache`` CLI args for *configuration*.

        Empty list when the device is unknown, has no API integration
        loaded, or has no cached IP available.
        """
        target_name = configuration.removesuffix(".yaml").removesuffix(".yml")
        device = next((d for d in self._scanner.devices if d.name == target_name), None)
        if device is None:
            return []
        # The CLI only consults the address cache through the API client;
        # non-API devices flash via a different path that wouldn't read it.
        if "api" not in device.loaded_integrations:
            return []
        return _build_address_cache_args(device, self._state_monitor)

    # ------------------------------------------------------------------
    # API commands — listing
    # ------------------------------------------------------------------

    @api_command("devices/list")
    async def list_devices(self, **kwargs: Any) -> DevicesResponse:
        """List all configured and importable devices."""
        await self._scanner.scan()
        configured = self._scanner.devices
        configured_names = {d.name for d in configured}

        importable = []
        for discovered in self.import_result.values():
            if discovered.device_name in configured_names:
                continue
            importable.append(
                AdoptableDevice(
                    name=discovered.device_name,
                    friendly_name=discovered.friendly_name or "",
                    package_import_url=discovered.package_import_url,
                    project_name=discovered.project_name,
                    project_version=discovered.project_version,
                    network=discovered.network,
                    ignored=discovered.device_name in self.ignored_devices,
                )
            )

        return DevicesResponse(configured=configured, importable=importable)

    @api_command("devices/get_states")
    async def get_device_states(self, **kwargs: Any) -> dict:
        """Get connectivity state for all devices."""
        return {d.configuration: d.state.value for d in self._scanner.devices}

    # ------------------------------------------------------------------
    # API commands — CRUD
    # ------------------------------------------------------------------

    @api_command("devices/create")
    async def create_device(
        self,
        *,
        name: str,
        board_id: str | None = None,
        ssid: str = "",
        psk: str = "",
        file_content: str | None = None,
        **kwargs: Any,
    ) -> WizardResponse:
        """
        Create a new device configuration.

        Three flows, decided by which arguments are provided:

        1. ``file_content`` given → write it as-is (user supplied full YAML).
        2. ``board_id`` given → generate a basic config from the board template.
        3. Neither → write a minimal stub the user fills in manually.

        After writing, we always try to derive a board_id by parsing
        the resulting YAML's platform/board/variant fields and matching
        against the catalog. The derived (or supplied) board_id is
        stored in metadata for later reference.
        """
        name = name.strip()
        if not name:
            msg = "name is required"
            raise ValueError(msg)

        filename = f"{name}.yaml"
        config_path = self._db.settings.rel_path(filename)

        if config_path.exists():
            msg = "File already exists"
            raise FileExistsError(msg)

        board = None
        if board_id:
            if self._db.boards:
                board = await self._db.boards.get_board(board_id=board_id)
            if board is None:
                msg = f"Unknown board: {board_id}"
                raise ValueError(msg)

        friendly = friendly_name_slugify(name)
        if file_content:
            yaml_content = file_content
        elif board:
            yaml_content = generate_device_yaml(name, friendly, board, ssid, psk)
        else:
            yaml_content = f"esphome:\n  name: {name}\n  friendly_name: {friendly}\n\n"

        # Derive board_id from YAML when not explicitly provided.
        # Mirrors the scanner's resolution chain: pio_board match first,
        # then platform+variant fallback for generic ``esp32:``-style
        # configs without a specific PlatformIO board id.
        parsed_platform = ""
        if not board_id and self._db.boards:
            parsed_platform, pio_board, variant = parse_platform_from_yaml(yaml_content)
            matched = None
            if pio_board:
                matched = self._db.boards.find_by_pio_board(pio_board, variant)
            if matched is None and parsed_platform:
                matched = self._db.boards.find_by_platform_variant(parsed_platform, variant)
            if matched:
                board = matched
                board_id = matched.id

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, config_path.write_text, yaml_content, "utf-8")

        def _init_storage() -> None:
            platform = str(board.esphome.platform) if board else parsed_platform
            storage = StorageJSON(
                storage_version=1,
                name=name,
                friendly_name=friendly,
                comment=None,
                esphome_version=None,
                src_version=None,
                address=f"{name}.local",
                web_port=None,
                target_platform=platform,
                build_path=None,
                firmware_bin_path=None,
                loaded_integrations=[],
                loaded_platforms=[],
                no_mdns=False,
            )
            storage_path = ext_storage_path(filename)
            storage_path.parent.mkdir(parents=True, exist_ok=True)
            storage.save(storage_path)

            if board_id:
                set_device_metadata(self._db.settings.config_dir, filename, board_id=board_id)

        await loop.run_in_executor(None, _init_storage)
        await self._scanner.scan()
        return WizardResponse(configuration=filename)

    @api_command("devices/update")
    async def update_device(
        self,
        *,
        name: str,
        friendly_name: str | None = None,
        comment: str | None = None,
        board_id: str | None = None,
        **kwargs: Any,
    ) -> UpdateDeviceResponse:
        """Update device metadata (sidecar JSON, not the YAML file)."""
        filename = f"{name}.yaml"
        loop = asyncio.get_running_loop()
        config_dir = self._db.settings.config_dir

        await loop.run_in_executor(
            None,
            lambda: set_device_metadata(
                config_dir,
                filename,
                board_id=board_id,
                friendly_name=friendly_name,
                comment=comment,
            ),
        )

        meta = get_device_metadata(config_dir, filename)
        return UpdateDeviceResponse(
            name=name,
            friendly_name=meta.get("friendly_name", name),
            comment=meta.get("comment"),
            board_id=meta.get("board_id"),
        )

    @api_command("devices/rename")
    async def rename_device(
        self,
        *,
        configuration: str,
        new_name: str,
        **kwargs: Any,
    ) -> dict[str, str]:
        """
        Rename a device configuration.

        Tries the ESPHome CLI first (authoritative for validated
        configs). Falls back to a file-level rename when the CLI
        refuses because the config doesn't validate yet — typical for
        a freshly-created empty config. Returns the new filename.
        """
        config_path = str(self._db.settings.rel_path(configuration))
        cmd = [*self._esphome_cmd, "rename", config_path, new_name]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate(input=b"y\n")
        exit_code = proc.returncode
        output = stdout.decode("utf-8", errors="replace")

        new_filename = f"{new_name}.yaml"
        if exit_code != 0:
            _LOGGER.info(
                "esphome rename failed (%s); falling back to manual rename",
                exit_code,
            )
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(None, self._manual_rename, configuration, new_name)
            except FileExistsError as exc:
                msg = f"A device named {new_filename} already exists"
                raise RuntimeError(msg) from exc
            except Exception as exc:
                _LOGGER.warning("Manual rename failed: %s", exc)
                tail = output.strip()[-500:]
                msg = f"Rename failed (exit {exit_code}): {tail}"
                raise RuntimeError(msg) from exc

        await self._scanner.scan()
        return {"configuration": new_filename}

    @api_command("devices/delete")
    async def delete_device(self, *, configuration: str, **kwargs: Any) -> None:
        """Delete a device and all associated files."""
        await self._delete_single(configuration)
        await self._scanner.scan()

    @api_command("devices/delete_bulk")
    async def delete_bulk(
        self, *, configurations: list[str], **kwargs: Any
    ) -> list[dict[str, Any]]:
        """
        Delete multiple devices at once.

        Returns one ``{configuration, success, error?}`` dict per device.
        """
        results: list[dict[str, Any]] = []
        for configuration in configurations:
            try:
                await self._delete_single(configuration)
                results.append({"configuration": configuration, "success": True})
            except Exception as exc:
                results.append(
                    {
                        "configuration": configuration,
                        "success": False,
                        "error": str(exc),
                    }
                )
        await self._scanner.scan()
        return results

    @api_command("devices/get_config")
    async def get_config(self, *, configuration: str, **kwargs: Any) -> str:
        """Read device config YAML."""
        path = self._db.settings.rel_path(configuration)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, path.read_text, "utf-8")

    @api_command("devices/update_config")
    async def update_config(self, *, configuration: str, content: str, **kwargs: Any) -> None:
        """Write device config YAML."""
        path = self._db.settings.rel_path(configuration)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, path.write_text, content, "utf-8")
        await self._scanner.scan()

    @api_command("devices/add_component")
    async def add_component(
        self,
        *,
        configuration: str,
        component_id: str,
        fields: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AddComponentResponse:
        """
        Add a component block to an existing device YAML.

        ``fields`` is a flat mapping of config-entry key → value. For
        NESTED config entries the value is itself a dict matching the
        nested entry's structure (recursive).
        """
        assert self._db.components is not None  # type narrowing
        component = await self._db.components.get_component(component_id=component_id)
        if component is None:
            msg = f"Unknown component: {component_id}"
            raise ValueError(msg)

        fields = fields or {}
        for entry in component.config_entries:
            if entry.required and entry.key not in fields:
                msg = f"Missing required field: {entry.key}"
                raise ValueError(msg)

        config_path = self._db.settings.rel_path(configuration)
        loop = asyncio.get_running_loop()
        existing = await loop.run_in_executor(None, config_path.read_text, "utf-8")
        new_yaml = merge_component_yaml(existing, component, fields)
        await loop.run_in_executor(None, config_path.write_text, new_yaml, "utf-8")
        await self._scanner.scan()

        return AddComponentResponse(yaml=new_yaml)

    @api_command("devices/import")
    async def import_device(
        self,
        *,
        name: str,
        project_name: str = "",
        package_import_url: str = "",
        friendly_name: str | None = None,
        encryption: str | None = None,
        **kwargs: Any,
    ) -> dict:
        """Import / adopt a discovered device."""
        if import_config is None:
            msg = "import_config not available in this ESPHome version"
            raise RuntimeError(msg)

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            import_config,
            self._db.settings.rel_path(f"{name}.yaml"),
            name,
            friendly_name,
            project_name,
            package_import_url,
            const.CONF_WIFI,
            encryption,
        )

        await self._scanner.scan()
        return {"configuration": f"{name}.yaml"}

    @api_command("devices/ignore")
    async def toggle_ignore(self, *, name: str, ignore: bool = True, **kwargs: Any) -> None:
        """Mark a discovered device as ignored / visible in the import list."""
        if ignore:
            self.ignored_devices.add(name)
        else:
            self.ignored_devices.discard(name)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._save_ignored_devices)

    # ------------------------------------------------------------------
    # API commands — per-connection streams (validate, logs)
    # ------------------------------------------------------------------

    @api_command("devices/validate")
    async def validate_config(
        self,
        *,
        configuration: str,
        client: Any = None,
        message_id: str = "",
        **kwargs: Any,
    ) -> None:
        """Validate a device YAML config. Streams output per-connection."""
        config_path = str(self._db.settings.rel_path(configuration))
        cmd = [*self._esphome_cmd, "config", config_path]
        await self._stream_subprocess(cmd, client, message_id)

    @api_command("devices/logs")
    async def stream_logs(
        self,
        *,
        configuration: str,
        port: str = "",
        client: Any = None,
        message_id: str = "",
        **kwargs: Any,
    ) -> None:
        """Stream live device logs. Per-connection, not queued."""
        config_path = str(self._db.settings.rel_path(configuration))
        cmd = [*self._esphome_cmd, "logs", config_path]
        if port:
            cmd.extend(["--device", port])
        await self._stream_subprocess(cmd, client, message_id)

    @api_command("devices/stop_stream")
    async def stop_stream(
        self,
        *,
        stream_id: str,
        client: Any = None,
        **kwargs: Any,
    ) -> dict:
        """
        Cancel a streaming command (``devices/logs`` or ``devices/validate``) on this connection.

        ``stream_id`` is the ``message_id`` returned when the streaming
        command was issued. Returns ``{"cancelled": True}`` if a matching
        in-flight stream was found; ``{"cancelled": False}`` otherwise
        (already finished, never registered, or no client context).
        """
        if client is None:
            return {"cancelled": False}
        return {"cancelled": client.cancel_stream(stream_id)}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_devices(self) -> list[Device]:
        """Bridge for the state monitor (``self._scanner.devices`` is a property)."""
        return self._scanner.devices

    def _resolve_device_metadata(self, config_dir: Path, filename: str) -> DeviceFileMetadata:
        """
        Resolve a device's persisted ``board_id``, ``ip``, and config hash.

        ``board_id`` priority:
          1. The metadata sidecar — set explicitly when the user
             picks a board through the UI, or backfilled by a
             previous scan.
          2. Parse the YAML's ``esphome.platform`` / ``board`` /
             ``variant`` and match by PlatformIO board id
             (``find_by_pio_board``).
          3. Same YAML — match by platform + variant
             (``find_by_platform_variant``). Picks up generic
             ``esp32: { variant: esp32c3 }``-style configs that don't
             name a specific PlatformIO ``board:``. Generic catalog
             entries are preferred so the dashboard tags these with
             the matching ``generic-esp32-c3`` rather than a random
             vendor board that shares the variant.

        On any successful YAML-derived match we persist the result to
        metadata so subsequent scans skip the YAML parse.

        ``ip`` is the last-known resolved address from the metadata
        sidecar (``""`` if never seen).

        ``expected_config_hash`` is the YAML's last-compiled
        ``CORE.config_hash``, written by the firmware controller after
        each successful compile; ``""`` when the device has never been
        compiled or the compile predates expected-hash tracking.
        """
        md = get_device_metadata(config_dir, filename)
        ip = str(md.get("ip", ""))
        expected_config_hash = str(md.get("expected_config_hash", ""))
        board_id = str(md.get("board_id", ""))
        if not board_id:
            board_id = self._derive_board_id_from_yaml(config_dir, filename)
        return DeviceFileMetadata(
            board_id=board_id, ip=ip, expected_config_hash=expected_config_hash
        )

    def _derive_board_id_from_yaml(self, config_dir: Path, filename: str) -> str:
        """Parse the device YAML and look up a matching catalog board, or ``""``."""
        if self._db.boards is None:
            return ""
        yaml_path = config_dir / filename
        try:
            yaml_content = yaml_path.read_text(encoding="utf-8")
        except OSError:
            return ""
        platform, pio_board, variant = parse_platform_from_yaml(yaml_content)

        matched = None
        if pio_board:
            matched = self._db.boards.find_by_pio_board(pio_board, variant)
        if matched is None and platform:
            matched = self._db.boards.find_by_platform_variant(platform, variant)
        if matched is None:
            return ""

        # Backfill metadata so future scans skip the YAML parse.
        try:
            set_device_metadata(config_dir, filename, board_id=matched.id)
        except Exception:
            _LOGGER.warning("Could not persist derived board_id for %s", filename)
        return matched.id

    def _on_scan_change(self, kind: ScanChange, device: Device) -> None:
        """Forward scanner changes onto the event bus."""
        event = {
            ScanChange.ADDED: EventType.DEVICE_ADDED,
            ScanChange.UPDATED: EventType.DEVICE_UPDATED,
            ScanChange.REMOVED: EventType.DEVICE_REMOVED,
        }[kind]
        self._db.bus.fire(event, {"device": device})

    def _on_state_change(self, name: str, state: DeviceState, source: str) -> None:
        """Forward state monitor updates onto the event bus."""
        device = next((d for d in self._scanner.devices if d.name == name), None)
        if device is None:
            return
        old_state = device.state
        device.state = state
        _LOGGER.info("Device %s: %s → %s (via %s)", name, old_state, state, source)
        self._db.bus.fire(EventType.DEVICE_STATE_CHANGED, {"device": device})

    def _on_ip_change(self, name: str, ip: str) -> None:
        """
        Forward IP updates onto the event bus and persist non-empty values.

        ``ip=""`` means the device dropped off mDNS — we keep the
        last-known IP on disk so the OTA address cache stays warm
        across the device's offline window. The DNS pre-resolve and
        next mDNS resolve will overwrite it on reconnect.
        """
        device = next((d for d in self._scanner.devices if d.name == name), None)
        if device is None:
            return
        if device.ip == ip:
            return
        device.ip = ip
        _LOGGER.debug("Device %s IP: %s", name, ip or "(cleared)")
        if ip:
            self._db.create_background_task(self._persist_device_ip_async(device.configuration, ip))
        self._db.bus.fire(EventType.DEVICE_UPDATED, {"device": device})

    async def _persist_device_ip_async(self, configuration: str, ip: str) -> None:
        """Save *ip* to the device-builder metadata sidecar."""
        loop = asyncio.get_running_loop()
        config_dir = self._db.settings.config_dir
        await loop.run_in_executor(
            None, lambda: set_device_metadata(config_dir, configuration, ip=ip)
        )

    def _on_version_change(self, name: str, version: str) -> None:
        """Apply a fresh ESPHome version observed via mDNS."""
        device = next((d for d in self._scanner.devices if d.name == name), None)
        if device is None:
            return
        if device.deployed_version == version:
            return

        # StorageJSON.load/save are blocking — push to a background task
        # so any error gets surfaced via the loop's exception handler.
        self._db.create_background_task(
            self._persist_storage_version_async(device.configuration, version)
        )

        old_version = device.deployed_version
        device.deployed_version = version
        device.update_available = bool(device.current_version and version != device.current_version)
        _LOGGER.info("Device %s version: %s → %s (via mdns)", name, old_version or "?", version)
        self._db.bus.fire(EventType.DEVICE_UPDATED, {"device": device})

    def _on_config_hash_change(self, name: str, config_hash: str) -> None:
        """
        Apply a running-firmware config hash observed via mDNS.

        Stores the hash on the in-memory device and, when both
        expected and deployed hashes are known, flips
        ``has_pending_changes`` to reflect the comparison so the
        dashboard can tell "device runs the latest compile" apart
        from "device has older firmware". Devices on firmware that
        predates the ``config_hash`` TXT broadcast never trigger this
        callback and stay on the legacy mtime check.
        """
        device = next((d for d in self._scanner.devices if d.name == name), None)
        if device is None:
            return
        if device.deployed_config_hash == config_hash:
            return
        old_hash = device.deployed_config_hash
        device.deployed_config_hash = config_hash
        # Mtime side stays with the periodic scanner poll so this
        # callback can stay off-disk and non-blocking. A YAML edit
        # between polls (~5s window) self-corrects on the next scan.
        if device.expected_config_hash:
            device.has_pending_changes = device.expected_config_hash != config_hash
        _LOGGER.info(
            "Device %s config_hash: %s → %s (via mdns)", name, old_hash or "?", config_hash
        )
        self._db.bus.fire(EventType.DEVICE_UPDATED, {"device": device})

    def _on_firmware_job_completed(self, event: Any) -> None:
        """
        Refresh a device's cached state after a successful firmware job.

        Without this hook, a freshly-flashed device keeps its stale
        ``has_pending_changes=True`` — the symptom users see as a
        still-orange "update pending" dot — because the disk scanner
        only re-evaluates when the YAML file's stat changes.

        COMPILE and INSTALL also recompute the YAML's
        ``expected_config_hash`` here so the next mDNS resolve can
        compare against the firmware's broadcast hash; UPLOAD doesn't
        recompile, so it reuses whatever the previous compile cached.
        """
        job = event.data.get("job")
        if job is None:
            return
        if getattr(job, "status", None) != JobStatus.COMPLETED:
            return
        job_type = getattr(job, "job_type", None)
        if job_type not in (JobType.COMPILE, JobType.UPLOAD, JobType.INSTALL):
            return
        configuration = getattr(job, "configuration", "")
        if not configuration:
            return
        recompute_hash = job_type in (JobType.COMPILE, JobType.INSTALL)
        self._db.create_background_task(
            self._refresh_after_firmware_job(configuration, recompute_hash=recompute_hash)
        )

    async def _refresh_after_firmware_job(
        self, configuration: str, *, recompute_hash: bool
    ) -> None:
        """
        Persist the YAML's freshly-compiled hash and reload the device.

        When *recompute_hash* is True, recomputes the YAML's
        ``CORE.config_hash`` and writes it to the metadata sidecar so
        the next mDNS resolve can compare against the firmware's
        broadcast. The device is always reloaded afterwards — even
        when hash computation is skipped or fails — so the mtime side
        of ``has_pending_changes`` still flips after a successful
        compile.
        """
        if recompute_hash:
            yaml_path = self._db.settings.rel_path(configuration)
            new_hash = await compute_yaml_config_hash(yaml_path)
            if new_hash:
                loop = asyncio.get_running_loop()
                config_dir = self._db.settings.config_dir
                await loop.run_in_executor(
                    None,
                    lambda: set_device_metadata(
                        config_dir, configuration, expected_config_hash=new_hash
                    ),
                )
                _LOGGER.debug("Stored expected_config_hash for %s: %s", configuration, new_hash)
        await self._scanner.reload(configuration)

    async def _persist_storage_version_async(self, configuration: str, version: str) -> None:
        """Update ``StorageJSON.esphome_version`` on disk if it differs."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._persist_storage_version, configuration, version)

    @staticmethod
    def _persist_storage_version(configuration: str, version: str) -> None:
        """Write *version* to ``StorageJSON.esphome_version`` if it differs."""
        storage_path = ext_storage_path(configuration)
        storage = StorageJSON.load(storage_path)
        if storage is None:
            return
        if storage.esphome_version == version:
            return
        previous = storage.esphome_version
        storage.esphome_version = version
        storage.save(storage_path)
        _LOGGER.debug(
            "Updated StorageJSON for %s with mdns version %s (was %s)",
            configuration,
            version,
            previous,
        )

    def _load_ignored_devices(self) -> None:
        storage_path = ignored_devices_storage_path()
        try:
            with storage_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
                self.ignored_devices = set(data.get("ignored_devices", []))
        except FileNotFoundError:
            pass

    def _save_ignored_devices(self) -> None:
        storage_path = ignored_devices_storage_path()
        with storage_path.open("w", encoding="utf-8") as f:
            json.dump({"ignored_devices": sorted(self.ignored_devices)}, f, indent=2)

    def _manual_rename(self, configuration: str, new_name: str) -> None:
        """File-level rename. Used when the ESPHome CLI refuses (invalid config)."""
        config_dir = self._db.settings.config_dir
        old_path = config_dir / configuration
        new_filename = f"{new_name}.yaml"
        new_path = config_dir / new_filename

        if not old_path.exists():
            msg = f"File not found: {configuration}"
            raise FileNotFoundError(msg)
        if new_path.exists():
            raise FileExistsError(new_filename)

        old_name = configuration.removesuffix(".yaml").removesuffix(".yml")
        content = old_path.read_text(encoding="utf-8")
        new_content = rewrite_esphome_name(content, old_name, new_name)
        new_path.write_text(new_content, encoding="utf-8")
        old_path.unlink()

        # Move StorageJSON alongside the YAML rename
        try:
            old_storage = ext_storage_path(configuration)
            new_storage = ext_storage_path(new_filename)
            if old_storage.exists():
                storage = StorageJSON.load(old_storage)
                if storage:
                    storage.name = new_name
                    if storage.friendly_name == old_name:
                        storage.friendly_name = new_name
                    storage.address = f"{new_name}.local"
                    new_storage.parent.mkdir(parents=True, exist_ok=True)
                    storage.save(new_storage)
                old_storage.unlink(missing_ok=True)
        except Exception:
            _LOGGER.warning("Could not update storage for %s", new_filename)

        # Move the sidecar metadata entry to the new filename
        try:
            old_meta = get_device_metadata(config_dir, configuration)
            if old_meta:
                meta_friendly = old_meta.get("friendly_name")
                set_device_metadata(
                    config_dir,
                    new_filename,
                    board_id=old_meta.get("board_id"),
                    friendly_name=(new_name if meta_friendly == old_name else meta_friendly),
                    comment=old_meta.get("comment"),
                    ip=old_meta.get("ip"),
                )
                remove_device_metadata(config_dir, configuration)
        except Exception:
            _LOGGER.warning("Could not move metadata for %s", new_filename)

    async def _delete_single(self, configuration: str) -> None:
        """Delete a single device and all associated files."""
        config_path = self._db.settings.rel_path(configuration)
        if not config_path.exists():
            msg = f"File not found: {configuration}"
            raise FileNotFoundError(msg)

        loop = asyncio.get_running_loop()
        config_dir = self._db.settings.config_dir

        def _delete_all() -> None:
            config_path.unlink(missing_ok=True)
            (config_dir / ".trash" / configuration).unlink(missing_ok=True)
            (config_dir / ".archive" / f"{configuration}.json").unlink(missing_ok=True)
            try:
                ext_storage_path(configuration).unlink(missing_ok=True)
            except OSError:
                _LOGGER.warning("Could not remove storage file for %s", configuration)
            try:
                remove_device_metadata(config_dir, configuration)
            except Exception:
                _LOGGER.warning("Could not remove metadata for %s", configuration)

        await loop.run_in_executor(None, _delete_all)

    async def _stream_subprocess(self, cmd: list[str], client: Any, message_id: str) -> None:
        """Run a CLI subprocess and stream its merged stdout/stderr to a single client.

        Registers the running task with the client so a peer ``devices/stop_stream``
        command (or a WS disconnect) can cancel it; cancellation kills the
        subprocess so it doesn't keep running detached.
        """
        # Register before the first await so an early ``stop_stream`` (during
        # subprocess spawn) still finds and cancels this task.
        task = asyncio.current_task()
        assert task is not None  # always running inside a Task
        client.register_stream(message_id, task)

        env = {**os.environ, "PLATFORMIO_FORCE_ANSI": "true"}
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            assert proc.stdout is not None
            async for line_bytes in proc.stdout:
                line = line_bytes.decode("utf-8", errors="replace").rstrip("\n\r")
                await client.send_event(message_id, "output", line)
            exit_code = await proc.wait()
        except asyncio.CancelledError:
            # Synchronous kill only — no awaits in the cancel path. The
            # ``finally`` block reaps the process and ``devices/stop_stream``
            # is what tells the frontend the cancel succeeded. ``proc`` may
            # be ``None`` if cancellation arrived before spawn returned.
            if proc is not None and proc.returncode is None:
                proc.kill()
            # Honour the cancellation contract — only swallow if no
            # outstanding cancel requests remain on this task.
            if (current := asyncio.current_task()) and current.cancelling():
                raise
            return
        finally:
            client.unregister_stream(message_id)
            if proc is not None and proc.returncode is None:
                # Reap so the transport closes cleanly; shielded so an
                # additional cancellation doesn't strand the subprocess.
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(proc.wait())

        await client.send_event(
            message_id, "result", {"success": exit_code == 0, "code": exit_code}
        )


def _build_address_cache_args(device: Device, monitor: DeviceStateMonitor | None) -> list[str]:
    """Build CLI cache args from the IPs we already have for *device*."""
    address = device.address
    if not address:
        return []

    # mDNS hostnames are case-insensitive and may carry a trailing dot;
    # normalise once so the CLI cache key matches what it'll look up.
    normalized = address.rstrip(".").lower()
    is_local = normalized.endswith(".local")

    # Preferred source per host type:
    #   .local  → zeroconf cache (mDNS-only, freshest while the browser is alive)
    #   non-.local → DNS cache populated by the ping sweep's pre-resolve pass
    # Either falls back to ``device.ip`` (the last-known resolved IP) so
    # an expired cache entry doesn't strip the cache args entirely.
    addresses: list[str] = []
    if monitor is not None:
        cached = (
            monitor.get_cached_addresses(address)
            if is_local
            else monitor.get_cached_dns_addresses(address)
        )
        if cached:
            addresses = list(cached)

    if not addresses and device.ip:
        addresses = [device.ip]

    if not addresses:
        return []

    cache_type = "mdns" if is_local else "dns"
    return [
        f"--{cache_type}-address-cache",
        f"{normalized}={','.join(sort_ip_addresses(addresses))}",
    ]
