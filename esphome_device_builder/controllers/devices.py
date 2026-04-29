"""Devices controller — device CRUD, file watching, CLI operations, state management."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any

from esphome import const, util
from esphome.zeroconf import AsyncEsphomeZeroconf

try:
    from icmplib import async_ping as icmp_ping
except ImportError:
    icmp_ping = None  # type: ignore[assignment]
from esphome.dashboard.util.text import friendly_name_slugify
from esphome.storage_json import StorageJSON, ext_storage_path, ignored_devices_storage_path

from ..helpers.api import api_command
from ..helpers.yaml import generate_component_yaml, rewrite_esphome_name
from ..models import (
    AddComponentResponse,
    AdoptableDevice,
    Device,
    DevicesResponse,
    DeviceState,
    EventType,
    UpdateDeviceResponse,
    WizardResponse,
)
from .config import (
    get_board_id,
    get_device_metadata,
    remove_device_metadata,
    set_device_metadata,
)

try:
    from esphome.config_helpers import import_config
except ImportError:
    import_config = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from ..device_builder import DeviceBuilder

_LOGGER = logging.getLogger(__name__)
_ESPHOME_CMD: list[str] = []  # resolved in start()

# Cache key for file change detection: (inode, device, mtime, size)
_CacheKey = tuple[int, int, float, int]
_ESPHOME_SERVICE_TYPE = "_esphomelib._tcp.local."
_PING_INTERVAL = 60  # seconds between ping sweeps
_PING_BATCH_SIZE = 10


def _generate_device_yaml(
    name: str,
    friendly_name: str,
    board: Any,
    ssid: str,
    psk: str,
) -> str:
    """Generate a complete device YAML config from a board definition.

    Produces the base config with platform settings, logging, API, OTA,
    and WiFi — the most common/sane defaults for a new device.
    """
    esphome_cfg = board.esphome
    lines: list[str] = []

    # Board reference comment
    board_label = board.name
    if board.manufacturer:
        board_label = f"{board.name} ({board.manufacturer})"
    lines.append(f"# Board: {board_label}")
    lines.append(f"# Definition: definitions/boards/{board.id}/manifest.yaml")
    lines.append("")

    # ESPHome core
    lines.append("esphome:")
    lines.append(f"  name: {name}")
    lines.append(f"  friendly_name: {friendly_name}")
    lines.append("")

    # Platform config
    # ESP32: variant + flash_size, board optional
    # All others: board is REQUIRED, no variant/flash_size
    platform = str(esphome_cfg.platform)
    hardware = board.hardware
    lines.append(f"{platform}:")

    if platform == "esp32":
        # ESP32 uses variant instead of board
        if esphome_cfg.variant:
            lines.append(f"  variant: {esphome_cfg.variant}")
        if hardware.flash_size:
            lines.append(f"  flash_size: {hardware.flash_size}")
        if esphome_cfg.framework:
            lines.append("  framework:")
            lines.append(f"    type: {esphome_cfg.framework}")
    else:
        # esp8266, rp2040, bk72xx, rtl87xx, ln882x, nrf52 — board is required
        lines.append(f"  board: {esphome_cfg.board}")

    lines.append("")

    # Logging
    lines.append("logger:")
    lines.append("")

    # Home Assistant API — unique encryption key per device
    api_key = base64.b64encode(secrets.token_bytes(32)).decode()
    lines.append("api:")
    lines.append("  encryption:")
    lines.append(f'    key: "{api_key}"')
    lines.append("")

    # OTA
    lines.append("ota:")
    lines.append("  - platform: esphome")
    lines.append("")

    # WiFi (only for boards that support it)
    connectivity = [c.value for c in board.hardware.connectivity] if board.hardware else []
    has_wifi = "wifi" in connectivity or not connectivity  # assume wifi if no connectivity data
    if has_wifi:
        lines.append("wifi:")
        if ssid:
            lines.append(f"  ssid: {ssid}")
            lines.append(f"  password: {psk}")
        else:
            lines.append("  ssid: !secret wifi_ssid")
            lines.append("  password: !secret wifi_password")
        lines.append("")

    return "\n".join(lines)


_PLATFORM_KEYS = frozenset({"esp32", "esp8266", "rp2040", "bk72xx", "rtl87xx", "ln882x", "nrf52"})


def _parse_platform_from_yaml(yaml_content: str) -> tuple[str, str, str]:
    """Parse YAML content and return ``(platform, pio_board, variant)``.

    Looks at top-level platform keys (esp32:, esp8266:, etc.) and reads
    the ``board:`` and ``variant:`` fields nested under them. Returns
    empty strings for fields that aren't present.
    """
    platform = ""
    pio_board = ""
    variant = ""
    in_platform = False

    for line in yaml_content.splitlines():
        if line and not line[0].isspace() and ":" in line:
            key = line.split(":")[0].strip()
            if key in _PLATFORM_KEYS:
                platform = key
                in_platform = True
            else:
                in_platform = False
            continue
        if not in_platform:
            continue
        stripped = line.strip()
        if stripped.startswith("board:"):
            pio_board = stripped.split(":", 1)[1].strip().strip('"').strip("'")
        elif stripped.startswith("variant:"):
            variant = stripped.split(":", 1)[1].strip().strip('"').strip("'")

    return platform, pio_board, variant


def _detect_platform_from_yaml(path: Path) -> str:
    """Quick scan of YAML to find the platform key (esp32, esp8266, etc.)."""
    try:
        platform, _, _ = _parse_platform_from_yaml(path.read_text(encoding="utf-8"))
        return platform
    except Exception:
        return ""


def _parse_esphome_meta(
    yaml_content: str,
) -> tuple[str | None, str | None, str | None]:
    """Parse the top-level ``esphome:`` block for ``{name, friendly_name, comment}``.

    Returns ``None`` for any field that isn't present in the YAML so callers
    can distinguish "key absent" (fall through to storage) from "explicit
    empty string" (user cleared the value).
    """
    name: str | None = None
    friendly_name: str | None = None
    comment: str | None = None
    in_esphome = False

    for line in yaml_content.splitlines():
        if line and not line[0].isspace() and ":" in line:
            key = line.split(":")[0].strip()
            in_esphome = key == "esphome"
            continue
        if not in_esphome:
            continue
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        for field, setter in (
            ("name", "name"),
            ("friendly_name", "friendly_name"),
            ("comment", "comment"),
        ):
            prefix = f"{field}:"
            if stripped.startswith(prefix):
                value = stripped[len(prefix) :].strip()
                # Strip inline `# comment` and matching quotes.
                if "#" in value and not (value.startswith('"') or value.startswith("'")):
                    value = value.split("#", 1)[0].rstrip()
                if (value.startswith('"') and value.endswith('"')) or (
                    value.startswith("'") and value.endswith("'")
                ):
                    value = value[1:-1]
                if setter == "name":
                    name = value
                elif setter == "friendly_name":
                    friendly_name = value
                else:
                    comment = value
                break

    return name, friendly_name, comment


def _load_device_from_storage(path: Path, board_id: str = "") -> Device:
    """Build a Device model from a YAML config file and its StorageJSON.

    User-editable fields (``name`` / ``friendly_name`` / ``comment``) come
    from the YAML when present so the dashboard reflects edits immediately,
    without having to wait for the next compile to refresh ``StorageJSON``.
    """
    filename = path.name
    storage = StorageJSON.load(ext_storage_path(filename))

    try:
        yaml_content = path.read_text(encoding="utf-8")
    except OSError:
        yaml_content = ""
    yaml_name, yaml_friendly, yaml_comment = _parse_esphome_meta(yaml_content)

    fallback_name = filename.removesuffix(".yml").removesuffix(".yaml")
    storage_name = storage.name if storage else None
    name = yaml_name or storage_name or fallback_name

    storage_friendly = storage.friendly_name if storage else None
    friendly_name = yaml_friendly if yaml_friendly is not None else (storage_friendly or name)

    storage_comment = storage.comment if storage else None
    comment = yaml_comment if yaml_comment is not None else storage_comment

    # Detect pending config changes
    has_pending = True  # default: needs compile
    if storage and storage.firmware_bin_path and storage.firmware_bin_path.exists():
        yaml_mtime = path.stat().st_mtime
        bin_mtime = storage.firmware_bin_path.stat().st_mtime
        has_pending = yaml_mtime > bin_mtime

    # Detect ESPHome version update available
    deployed = storage.esphome_version or "" if storage else ""
    update_available = bool(deployed and deployed != const.__version__)

    # Platform: from StorageJSON if compiled, otherwise parse from YAML
    target_platform = ""
    if storage and storage.target_platform:
        target_platform = storage.target_platform
    else:
        target_platform = _detect_platform_from_yaml(path)

    return Device(
        name=name,
        friendly_name=friendly_name,
        configuration=filename,
        comment=comment,
        board_id=board_id,
        target_platform=target_platform,
        address=storage.address or "" if storage else "",
        web_port=storage.web_port if storage else None,
        current_version=const.__version__,
        deployed_version=deployed,
        loaded_integrations=sorted(storage.loaded_integrations) if storage else [],
        has_pending_changes=has_pending,
        update_available=update_available,
    )


# ---------------------------------------------------------------------------
# Devices controller
# ---------------------------------------------------------------------------


class DevicesController:
    """Manage device configurations, file watching, and CLI operations."""

    def __init__(self, device_builder: DeviceBuilder) -> None:
        self._db = device_builder
        self._devices: dict[Path, Device] = {}
        self._cache_keys: dict[Path, _CacheKey] = {}
        self._scan_lock = asyncio.Lock()

        # Device connectivity state
        self._state_source: dict[str, str] = {}  # device name → "mdns" | "ping"

        # Discovery state
        self.import_result: dict[str, Any] = {}
        self.ignored_devices: set[str] = set()

        # mDNS
        self._zeroconf: AsyncEsphomeZeroconf | None = None
        self._mdns_browser: Any = None

    async def start(self) -> None:
        """Initialize — load state, scan files, start discovery."""
        global _ESPHOME_CMD
        from .firmware import _find_esphome_cmd

        _ESPHOME_CMD = _find_esphome_cmd()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._load_ignored_devices)
        await self.scan_devices()
        _LOGGER.info("Devices controller started — %d devices loaded", len(self._devices))

        # Start mDNS browser for device discovery
        await self._start_mdns_browser()

        # Start ping sweep as fallback
        self._db.create_background_task(self._ping_loop())

    async def poll(self) -> None:
        """Poll for file changes."""
        await self._request_scan()

    # ------------------------------------------------------------------
    # Device connectivity state
    # ------------------------------------------------------------------

    def _find_device_by_name(self, name: str) -> Device | None:
        """Find a loaded device by its ESPHome name."""
        for device in self._devices.values():
            if device.name == name:
                return device
        return None

    def _set_device_state(self, name: str, state: DeviceState, source: str) -> None:
        """Update a device's connectivity state with source priority.

        mDNS always wins. Ping only sets state when mDNS hasn't resolved it.
        Fires DEVICE_STATE_CHANGED event if state actually changes.
        """
        device = self._find_device_by_name(name)
        if device is None:
            _LOGGER.debug("Device %s not found in loaded devices — ignoring state update", name)
            return
        # mDNS always wins — ping cannot override mDNS
        current_source = self._state_source.get(name, "unknown")
        if source == "ping" and current_source == "mdns":
            return
        if device.state == state:
            return
        old_state = device.state
        device.state = state
        self._state_source[name] = source
        _LOGGER.info("Device %s: %s → %s (via %s)", name, old_state, state, source)
        self._db.bus.fire(EventType.DEVICE_STATE_CHANGED, {"device": device})

    # ------------------------------------------------------------------
    # mDNS browser
    # ------------------------------------------------------------------

    async def _start_mdns_browser(self) -> None:
        """Start the mDNS browser for ESPHome device discovery."""
        try:
            from zeroconf import ServiceStateChange
            from zeroconf.asyncio import AsyncServiceBrowser

            self._zeroconf = AsyncEsphomeZeroconf()

            loop = asyncio.get_running_loop()

            def _on_service_state_change(
                zeroconf: Any, service_type: str, name: str, state_change: ServiceStateChange
            ) -> None:
                # mDNS name format: "my-device._esphomelib._tcp.local."
                # ESPHome uses hyphens in mDNS, underscores in YAML config
                device_name = name.split(".")[0].replace("-", "_")
                _LOGGER.debug("mDNS: %s %s (raw: %s)", state_change, device_name, name)

                # zeroconf callbacks run on a different thread —
                # schedule state updates on the event loop
                if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
                    loop.call_soon_threadsafe(
                        self._set_device_state, device_name, DeviceState.ONLINE, "mdns"
                    )
                elif state_change == ServiceStateChange.Removed:
                    loop.call_soon_threadsafe(
                        self._set_device_state, device_name, DeviceState.OFFLINE, "mdns"
                    )
                    self._state_source.pop(device_name, None)

            self._mdns_browser = AsyncServiceBrowser(
                self._zeroconf.zeroconf,
                _ESPHOME_SERVICE_TYPE,
                handlers=[_on_service_state_change],
            )
            _LOGGER.info("mDNS browser started for %s", _ESPHOME_SERVICE_TYPE)
        except Exception:
            _LOGGER.exception("Could not start mDNS browser — device discovery limited to ping")

    # ------------------------------------------------------------------
    # Ping sweep (fallback)
    # ------------------------------------------------------------------

    async def _ping_loop(self) -> None:
        """Periodically ping devices not already discovered by mDNS."""
        try:
            while True:
                await asyncio.sleep(_PING_INTERVAL)
                await self._ping_sweep()
        except asyncio.CancelledError:
            pass

    async def _ping_sweep(self) -> None:
        """Ping all devices not already marked online by mDNS."""
        if icmp_ping is None:
            return

        devices_to_ping = [
            d
            for d in self._devices.values()
            if d.address and self._state_source.get(d.name, "unknown") != "mdns"
        ]

        if not devices_to_ping:
            return

        _LOGGER.debug("Pinging %d devices", len(devices_to_ping))

        # Ping in batches
        for i in range(0, len(devices_to_ping), _PING_BATCH_SIZE):
            batch = devices_to_ping[i : i + _PING_BATCH_SIZE]
            tasks = [self._ping_device(d) for d in batch]
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _ping_device(self, device: Device) -> None:
        """Ping a single device and update state."""
        try:
            result = await icmp_ping(device.address, count=1, timeout=3, privileged=False)
            if result.is_alive:
                self._set_device_state(device.name, DeviceState.ONLINE, "ping")
            else:
                self._set_device_state(device.name, DeviceState.OFFLINE, "ping")
        except Exception:  # noqa: S110
            # Ping failed (permissions, network error) — don't change state
            pass

    # ------------------------------------------------------------------
    # File scanning
    # ------------------------------------------------------------------

    def get_devices(self) -> list[Device]:
        """Get all loaded devices."""
        return list(self._devices.values())

    async def _request_scan(self) -> None:
        """Request a device scan. Waits for any running scan to finish first."""
        await self.scan_devices()

    async def scan_devices(self) -> None:
        """Scan the config folder for YAML file changes."""
        async with self._scan_lock:
            await self._do_scan()

    async def _do_scan(self) -> None:
        loop = asyncio.get_running_loop()
        config_dir = self._db.settings.config_dir
        bus = self._db.bus

        # Get current state of files on disk
        path_to_cache_key = await loop.run_in_executor(None, self._get_path_to_cache_key)

        old_paths = set(self._devices.keys())
        new_paths = set(path_to_cache_key.keys())

        removed_paths = old_paths - new_paths
        added_paths = new_paths - old_paths
        possibly_updated = old_paths & new_paths

        # Detect updated files (cache key changed)
        updated_paths = {
            p for p in possibly_updated if path_to_cache_key[p] != self._cache_keys.get(p)
        }

        # Load new and updated devices from disk
        paths_to_load = added_paths | updated_paths
        if paths_to_load:
            devices_loaded = await loop.run_in_executor(
                None, self._load_devices, paths_to_load, config_dir
            )
            for path, device in devices_loaded.items():
                self._devices[path] = device
                self._cache_keys[path] = path_to_cache_key[path]
                event = EventType.DEVICE_ADDED if path in added_paths else EventType.DEVICE_UPDATED
                bus.fire(event, {"device": device})

        # Remove deleted devices
        for path in removed_paths:
            device = self._devices.pop(path, None)  # type: ignore[arg-type]
            self._cache_keys.pop(path, None)  # type: ignore[arg-type]
            if device:
                bus.fire(EventType.DEVICE_REMOVED, {"device": device})

    def _load_devices(self, paths: set[Path], config_dir: Path) -> dict[Path, Device]:
        """Load Device models from disk (runs in executor)."""
        result: dict[Path, Device] = {}
        for path in paths:
            try:
                board_id = get_board_id(config_dir, path.name)
                result[path] = _load_device_from_storage(path, board_id)
            except Exception:
                _LOGGER.warning("Failed to load device from %s", path.name)
        return result

    def _get_path_to_cache_key(self) -> dict[Path, _CacheKey]:
        """Scan disk for YAML files and build cache keys from the YAML file itself."""
        result: dict[Path, _CacheKey] = {}
        for file in util.list_yaml_files([self._db.settings.config_dir]):
            try:
                stat = file.stat()
            except OSError:
                continue
            result[file] = (stat.st_ino, stat.st_dev, stat.st_mtime, stat.st_size)
        return result

    # ------------------------------------------------------------------
    # Ignored devices
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # API commands — device listing
    # ------------------------------------------------------------------

    @api_command("devices/list")
    async def list_devices(self, **kwargs: Any) -> DevicesResponse:
        """List all configured and importable devices."""
        await self._request_scan()
        configured = self.get_devices()
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
        return {d.configuration: d.state.value for d in self._devices.values()}

    # ------------------------------------------------------------------
    # API commands — device CRUD
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
        """Create a new device configuration.

        Three flows, decided by which arguments are provided:

        1. ``file_content`` given → write it as-is (user supplied full YAML).
        2. ``board_id`` given → generate a basic config from the board template.
        3. Neither → write a minimal stub the user fills in manually.

        After writing, we always try to derive a board_id by parsing the
        resulting YAML's platform/board/variant fields and matching against
        the catalog. The derived (or supplied) board_id is stored in metadata
        for later reference.
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

        # Resolve the board if explicitly given
        board = None
        if board_id:
            if self._db.boards:
                board = await self._db.boards.get_board(board_id=board_id)
            if board is None:
                msg = f"Unknown board: {board_id}"
                raise ValueError(msg)

        # Compose the YAML content
        friendly = friendly_name_slugify(name)
        if file_content:
            yaml_content = file_content
        elif board:
            yaml_content = _generate_device_yaml(name, friendly, board, ssid, psk)
        else:
            yaml_content = f"esphome:\n  name: {name}\n  friendly_name: {friendly}\n\n"

        # Derive board_id from YAML when not explicitly provided
        parsed_platform = ""
        if not board_id and self._db.boards:
            parsed_platform, pio_board, variant = _parse_platform_from_yaml(yaml_content)
            if pio_board:
                matched = self._db.boards.find_by_pio_board(pio_board, variant)
                if matched:
                    board = matched
                    board_id = matched.id

        loop = asyncio.get_running_loop()

        def _write() -> None:
            config_path.write_text(yaml_content, encoding="utf-8")

        await loop.run_in_executor(None, _write)

        # Pre-create StorageJSON so device metadata is available immediately
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

        await self._request_scan()
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
        """Update device metadata."""
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
        """Rename a device.

        Tries the ESPHome CLI first (authoritative for validated configs), and
        falls back to a file-level rename when the CLI refuses because the
        config doesn't validate yet (e.g. a freshly created empty config).
        Returns the new configuration filename.
        """
        config_path = str(self._db.settings.rel_path(configuration))
        cmd = [*_ESPHOME_CMD, "rename", config_path, new_name]

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

        await self._request_scan()
        return {"configuration": new_filename}

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

    @api_command("devices/delete")
    async def delete_device(self, *, configuration: str, **kwargs: Any) -> None:
        """Delete a device and all associated files."""
        await self._delete_single(configuration)
        await self._request_scan()

    @api_command("devices/delete_bulk")
    async def delete_bulk(
        self, *, configurations: list[str], **kwargs: Any
    ) -> list[dict[str, Any]]:
        """Delete multiple devices at once.

        Returns a result per device: {configuration, success, error?}.
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
        await self._request_scan()
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
        await self._request_scan()

    @api_command("devices/add_component")
    async def add_component(
        self,
        *,
        configuration: str,
        component_id: str,
        fields: dict[str, Any] | None = None,
        sub_entities: dict[str, dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AddComponentResponse:
        """Add a component to a device configuration."""
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

        yaml_block = generate_component_yaml(component, fields, sub_entities)

        config_path = self._db.settings.rel_path(configuration)
        loop = asyncio.get_running_loop()
        existing = await loop.run_in_executor(None, config_path.read_text, "utf-8")
        new_yaml = existing.rstrip() + "\n\n" + yaml_block + "\n"
        await loop.run_in_executor(None, config_path.write_text, new_yaml, "utf-8")
        await self._request_scan()

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
        """Import/adopt a discovered device."""
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

        await self._request_scan()
        return {"configuration": f"{name}.yaml"}

    @api_command("devices/ignore")
    async def toggle_ignore(self, *, name: str, ignore: bool = True, **kwargs: Any) -> None:
        """Mark a device as ignored/visible in the import list."""
        if ignore:
            self.ignored_devices.add(name)
        else:
            self.ignored_devices.discard(name)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._save_ignored_devices)

    # ------------------------------------------------------------------
    # YAML validation and live log streaming (per-connection, not queued)
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
        cmd = [*_ESPHOME_CMD, "config", config_path]
        env = {**os.environ, "PLATFORMIO_FORCE_ANSI": "true"}

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )

        assert proc.stdout is not None  # type narrowing
        async for line_bytes in proc.stdout:
            line = line_bytes.decode("utf-8", errors="replace").rstrip("\n\r")
            await client.send_event(message_id, "output", line)

        exit_code = await proc.wait()
        await client.send_event(
            message_id, "result", {"success": exit_code == 0, "code": exit_code}
        )

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
        cmd = [*_ESPHOME_CMD, "logs", config_path]
        if port:
            cmd.extend(["--device", port])
        env = {**os.environ, "PLATFORMIO_FORCE_ANSI": "true"}

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )

        assert proc.stdout is not None  # type narrowing
        async for line_bytes in proc.stdout:
            line = line_bytes.decode("utf-8", errors="replace").rstrip("\n\r")
            await client.send_event(message_id, "output", line)

        exit_code = await proc.wait()
        await client.send_event(
            message_id, "result", {"success": exit_code == 0, "code": exit_code}
        )
