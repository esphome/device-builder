"""Devices controller — device CRUD, file watching, CLI operations, state management."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from esphome import const
from esphome.components.dashboard_import import import_config
from esphome.dashboard.util.text import friendly_name_slugify
from esphome.helpers import sort_ip_addresses
from esphome.storage_json import StorageJSON, ext_storage_path, ignored_devices_storage_path

from ..helpers.api import CommandError, api_command
from ..helpers.config_hash import compute_yaml_config_hash, read_build_info_hash
from ..helpers.device_yaml import (
    generate_device_yaml,
    get_api_encryption_key,
    load_device_yaml,
    parse_platform_from_yaml,
)
from ..helpers.hostname import is_local_hostname, normalize_hostname
from ..helpers.json import JSONDecodeError, dumps_indent, loads
from ..helpers.subprocess import create_subprocess_exec
from ..helpers.yaml import merge_component_yaml, rewrite_esphome_name
from ..models import (
    AddComponentResponse,
    AdoptableDevice,
    Device,
    DevicesResponse,
    DeviceState,
    ErrorCode,
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
    from .components import _FeaturedRecord

_LOGGER = logging.getLogger(__name__)


class DevicesController:
    """Manage device configurations, file watching, and CLI operations."""

    def __init__(self, device_builder: DeviceBuilder) -> None:
        self._db = device_builder
        self._esphome_cmd: list[str] = []
        # Unsubscribe handle for the firmware-job-completion listener
        # wired up in start(); held so stop() can detach cleanly.
        self._unsub_job_completed: Any = None

        # Discovery / import state. Keyed by ``device.name`` so the
        # WebSocket layer and ``devices/ignore`` can address entries
        # without juggling full mDNS service-instance names. Filled by
        # ``DeviceStateMonitor`` callbacks.
        self.import_result: dict[str, AdoptableDevice] = {}
        self.ignored_devices: set[str] = set()

        # Background ``--only-generate`` bookkeeping. ``--only-generate``
        # validates a YAML and writes its ``StorageJSON`` without doing
        # a real build; we trigger it whenever a YAML is saved or
        # first-seen with no compile output. Three guards stop us from
        # spinning:
        #   * ``_regenerate_pending`` — configurations already in flight
        #     (scheduled but not yet finished). Skip duplicate schedules.
        #   * ``_regenerate_failed`` — YAMLs whose last attempt failed.
        #     Don't retry until the file changes (cleared on
        #     ``ScanChange.UPDATED``).
        #   * ``_regenerate_lock`` — serialises the actual subprocess
        #     so we don't spawn N esphome compiles in parallel.
        self._regenerate_pending: set[str] = set()
        self._regenerate_failed: set[str] = set()
        self._regenerate_lock = asyncio.Lock()

        self._scanner = DeviceScanner(
            config_dir=self._db.settings.config_dir,
            get_metadata=self._resolve_device_metadata,
            on_change=self._on_scan_change,
        )
        self._state_monitor = DeviceStateMonitor(
            get_devices=self._get_devices,
            get_devices_by_name=self._scanner.get_by_name,
            on_state_change=self._on_state_change,
            on_ip_change=self._on_ip_change,
            on_version_change=self._on_version_change,
            on_config_hash_change=self._on_config_hash_change,
            on_api_encryption_change=self._on_api_encryption_change,
            on_importable_added=self._on_importable_added,
            on_importable_removed=self._on_importable_removed,
            is_ignored=self.ignored_devices.__contains__,
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
        # ``import_result`` is already pre-filtered against configured
        # devices when the discovery callback fires; this guard catches
        # the race where a YAML appeared between the callback and this
        # listing.
        importable = [d for d in self.import_result.values() if d.name not in configured_names]
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
        # ``_scanner.scan`` fires ``_on_scan_change(ADDED)`` for the
        # new YAML, and that callback already runs ``probe_device`` —
        # don't double-probe here. ``file_content`` may carry an
        # ``esphome.name`` that differs from the URL ``name``, in
        # which case the scan-change handler probes the YAML's name
        # (the right one) and an explicit second probe here would
        # target the wrong service.
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

        Validity gates which strategy we use, because the two paths
        have very different rollback semantics on failure and we MUST
        keep the user able to reach the device under its old name
        whenever the rename can't complete:

        - **Config doesn't validate** (typical for a freshly-created
          empty config that the user hasn't filled in yet). The
          ``esphome rename`` CLI refuses to touch it. Fall back to a
          pure file-level rename — there's no firmware on the device
          yet, so there's nothing to roll back from.
        - **Config validates**. Run ``esphome --dashboard rename`` and
          let it own the full atomic rename: write the new YAML,
          re-validate, ``esphome run`` to compile + install + verify,
          and then drop the old YAML only on install success. If
          install fails the CLI unlinks its newly-written YAML and
          returns non-zero — the old file (and the device's old
          hostname) stay intact so the user can fix things and try
          again. We DELIBERATELY do not fall back to a file-level
          rename here: the legacy dashboard had exactly that bug, and
          a half-flashed device with the YAML already pointing at the
          new name leaves nothing to mDNS-resolve when the user goes
          to retry.

        Returns the new filename. Errors propagate verbatim (with the
        last lines of ``esphome rename``'s output appended) so the
        frontend can show a meaningful message.
        """
        config_path = str(self._db.settings.rel_path(configuration))
        new_filename = f"{new_name}.yaml"

        # Reject up-front if the target filename is already in use.
        # The manual-rename branch already checks ``new_path.exists()``
        # itself, but the CLI ``esphome rename`` path does *not* — it
        # blindly ``write_text``s the new YAML and then OTA-installs
        # it, so a collision would silently overwrite the unrelated
        # device's config and flash that firmware to the wrong device.
        if new_filename != configuration:
            new_path = self._db.settings.rel_path(new_filename)
            if new_path.exists():
                msg = f"A device named {new_filename} already exists"
                raise CommandError(ErrorCode.INVALID_ARGS, msg)

        if not await self._yaml_validates(config_path):
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(None, self._manual_rename, configuration, new_name)
            except FileExistsError as exc:
                msg = f"A device named {new_filename} already exists"
                raise CommandError(ErrorCode.INVALID_ARGS, msg) from exc
            except Exception as exc:
                _LOGGER.warning("Manual rename failed: %s", exc)
                msg = f"Rename failed: {exc}"
                raise CommandError(ErrorCode.INTERNAL_ERROR, msg) from exc
            await self._scanner.scan()
            return {"configuration": new_filename, "job": None}

        # Validated configs route through the firmware queue so the
        # compile + install (which is what ``esphome rename`` does
        # internally) shows up alongside other firmware tasks with
        # live output instead of running silently in the background.
        if self._db.firmware is None:
            msg = "Firmware controller is unavailable"
            raise CommandError(ErrorCode.INTERNAL_ERROR, msg)
        job = await self._db.firmware.rename(configuration=configuration, new_name=new_name)
        return {"configuration": new_filename, "job": job.to_dict()}

    async def _yaml_validates(self, config_path: str) -> bool:
        """``esphome config`` precheck.

        Decides between the file-level fallback (for empty / broken
        configs that ``esphome rename`` would refuse) and the full
        ``esphome rename`` flow (which compiles + installs).

        Treats only a clean non-zero exit as "doesn't validate".
        Anything that prevents the precheck from running to
        completion — missing CLI, permission errors, etc. — bubbles
        up as a ``CommandError(INTERNAL_ERROR)``: silently treating
        those as "invalid" would route the rename into the file-level
        fallback even when the YAML *does* validate, recreating the
        very footgun (rename without a successful flash) we're
        trying to avoid.
        """
        try:
            proc = await create_subprocess_exec(
                *self._esphome_cmd,
                "--dashboard",
                "config",
                config_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            return await proc.wait() == 0
        except Exception as exc:
            _LOGGER.warning("YAML precheck failed to run for %s: %s", config_path, exc)
            msg = f"Could not validate {config_path}: {exc}"
            raise CommandError(ErrorCode.INTERNAL_ERROR, msg) from exc

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
        # Refresh ``StorageJSON`` so address / loaded_integrations /
        # config_hash etc. reflect the new YAML without waiting for a
        # full compile. Mirrors the upstream dashboard's
        # ``async_schedule_storage_json_update`` (called from its
        # ``EditRequestHandler`` after writing the YAML).
        self._schedule_storage_regenerate(configuration)

    def _schedule_storage_regenerate(self, configuration: str) -> None:
        """
        Run ``esphome compile --only-generate <yaml>`` in the background.

        ``--only-generate`` walks ESPHome's full config validation
        pipeline (resolving ``!secret`` / ``!include`` / packages /
        ``dashboard_import``) and writes the resulting StorageJSON
        without doing a real build. That populates ``address``,
        ``loaded_integrations``, ``target_platform``, etc. for devices
        that have never been compiled (the typical "wr2-test was just
        added and shows UNKNOWN forever" path) and refreshes them
        whenever the YAML changes.

        Three guards keep this from running away:
        * ``_regenerate_pending`` skips duplicate schedules for a
          configuration that's already in flight.
        * ``_regenerate_failed`` skips YAMLs whose last attempt
          failed; entries are cleared in ``_on_scan_change`` when the
          file's cache key changes (i.e. the user actually edited it).
        * ``_regenerate_lock`` serialises the subprocess itself so we
          never spawn more than one esphome compile at a time.

        Fire-and-forget: a follow-up ``_scanner.reload(configuration)``
        on success picks up the new storage and re-emits a
        ``DEVICE_UPDATED`` event so the frontend reflects the new
        address / integrations.
        """
        if not self._esphome_cmd:
            return  # ``start()`` hasn't run yet — skip the regenerate.
        if configuration in self._regenerate_pending:
            return  # already scheduled, don't queue a duplicate.
        if configuration in self._regenerate_failed:
            # Last attempt failed and the YAML hasn't changed since;
            # rerunning would just produce the same error and burn a
            # subprocess. Wait for an UPDATED scan to clear the marker.
            return

        async def _run() -> None:
            self._regenerate_pending.add(configuration)
            try:
                async with self._regenerate_lock:
                    config_path = str(self._db.settings.rel_path(configuration))
                    cmd = [
                        *self._esphome_cmd,
                        "--dashboard",
                        "compile",
                        "--only-generate",
                        config_path,
                    ]
                    try:
                        proc = await create_subprocess_exec(
                            *cmd,
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        _, stderr = await proc.communicate()
                    except Exception:
                        _LOGGER.debug(
                            "Storage regenerate spawn failed for %s",
                            configuration,
                            exc_info=True,
                        )
                        self._regenerate_failed.add(configuration)
                        return
                    if proc.returncode != 0:
                        _LOGGER.debug(
                            "Storage regenerate for %s exited %s: %s",
                            configuration,
                            proc.returncode,
                            stderr.decode(errors="replace").strip()[:500],
                        )
                        self._regenerate_failed.add(configuration)
                        return
                    # ``--only-generate`` writes build_info.json with
                    # the canonical config_hash before exiting, same as
                    # a real compile. Persist it to the metadata
                    # sidecar so the drawer can show "Local: <hash>"
                    # before the first real flash.
                    await self._persist_expected_config_hash(configuration)
                    await self._scanner.reload(configuration)
            finally:
                self._regenerate_pending.discard(configuration)

        self._db.create_background_task(_run())

    @api_command("devices/get_api_key")
    async def get_api_key(self, *, configuration: str, **kwargs: Any) -> dict[str, str]:
        """
        Return the resolved Native API encryption key for *configuration*.

        Uses ESPHome's own YAML loader so ``!secret`` references and
        substitutions resolve the same way they would at compile time —
        the regex-on-raw-YAML approach a frontend has access to gives up
        whenever the user pulls the key from ``secrets.yaml`` or hides
        it behind a ``${api_key}`` substitution.

        ``{"key": "<base64 32-byte>"}`` on success; ``{"key": ""}`` when
        the device has no ``api:`` block, no ``encryption`` key, or YAML
        loading fails. Callers treat the empty value as the "open the
        editor and check" signal.
        """
        path = self._db.settings.rel_path(configuration)
        loop = asyncio.get_running_loop()
        config = await loop.run_in_executor(None, load_device_yaml, path)
        return {"key": get_api_encryption_key(config)}

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

        Featured-component ids (``featured.<board>.<local>``) are
        recognised here: the backend resolves them to the underlying
        catalog component, validates user input against the manifest's
        ``locked`` / ``suggestions`` constraints, and merges the
        manifest's preset values into ``fields`` before delegating to
        the regular merge logic.
        """
        assert self._db.components is not None  # type narrowing

        fields = dict(fields or {})
        underlying_component_id = component_id

        if component_id.startswith("featured."):
            record = self._db.components.get_featured_record(component_id)
            if record is None:
                msg = f"Unknown featured component: {component_id}"
                raise ValueError(msg)
            underlying_component_id = record.underlying_id
            fields = _apply_featured_presets(record, fields)

        component = await self._db.components.get_component(component_id=underlying_component_id)
        if component is None:
            msg = f"Unknown component: {underlying_component_id}"
            raise ValueError(msg)

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
        configuration = f"{name}.yaml"
        path = self._db.settings.rel_path(configuration)
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None,
                import_config,
                path,
                name,
                friendly_name,
                project_name,
                package_import_url,
                const.CONF_WIFI,
                encryption,
            )
        except FileExistsError as exc:
            # ``import_config`` refuses to overwrite an existing YAML.
            # Surface this as a user-facing error so the dialog can
            # show "Configuration <file> already exists" instead of
            # the WS layer's generic "Command failed".
            msg = f"Configuration {configuration} already exists"
            raise CommandError(ErrorCode.INVALID_ARGS, msg) from exc

        # Picking up the new YAML is best-effort — if the scanner
        # hiccups (e.g. a transient stat error on a network mount),
        # the next periodic scan will catch it. We've already written
        # the YAML, so failing the whole command here would lie to
        # the user and trip a follow-up FileExistsError if they retry.
        try:
            await self._scanner.scan()
        except Exception:
            _LOGGER.exception("Scan after import failed; will pick up on next poll")

        # Drop the discovery banner entry: the device is now configured,
        # so it shouldn't continue to show up under "Discovered". The
        # importable cache key is the device's mDNS-advertised name,
        # which usually matches the user-chosen YAML name but may
        # differ (e.g. they edited the MAC suffix off). Match by
        # ``package_import_url`` so we always find the right entry,
        # and remember the cached name so we can use it for the
        # zeroconf-cache lookup below — the device is broadcasting
        # under that name, not the YAML name.
        cached_names = [
            n for n, d in self.import_result.items() if d.package_import_url == package_import_url
        ]
        for cached_name in cached_names:
            self._on_importable_removed(cached_name)
        mdns_name = cached_names[0] if cached_names else name

        # Skip-the-wait state seed. We just adopted a device that was
        # advertising on mDNS milliseconds ago, so the next ping sweep
        # would only confirm what zeroconf already knew. Pull the
        # cached IP out of zeroconf — keyed by the mDNS-advertised
        # name, not the user's chosen YAML name — and apply both
        # ONLINE and the address right away so the new card lands
        # online instead of blinking through OFFLINE for ~10s.
        self._state_monitor.apply(name, DeviceState.ONLINE, "mdns", claim=True)
        cached = self._state_monitor.get_cached_addresses(f"{mdns_name}.local")
        if cached:
            self._state_monitor.apply_ip(name, cached[0])
        # Eagerly probe the esphomelib service so the new card lands
        # with version / config_hash / api_encryption populated, not
        # just IP. The device on the network is still broadcasting
        # under its factory-firmware ``mdns_name`` (the user may have
        # picked a different YAML name during adoption), so look up
        # the service under that name but apply the result against
        # the configured device's chosen name. Cache hit returns
        # synchronously; otherwise the probe runs as a fire-and-
        # forget task whose results land via the same
        # browser-callback path. The ``_on_scan_change`` handler
        # also probes when the scan picked up the new YAML, but it
        # uses the YAML name only — for adoption that name has no
        # mDNS broadcast yet, so this explicit call covers the
        # rename-during-adopt case.
        self._state_monitor.probe_device(name, service_name=mdns_name)
        return {"configuration": configuration}

    @api_command("devices/ignore")
    async def toggle_ignore(self, *, name: str, ignore: bool = True, **kwargs: Any) -> None:
        """Mark a discovered device as ignored / visible in the import list."""
        if ignore:
            self.ignored_devices.add(name)
        else:
            self.ignored_devices.discard(name)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._save_ignored_devices)
        # Mirror the new flag onto the cached AdoptableDevice and
        # re-publish ADDED so subscribed frontends update the badge
        # without waiting for a full re-discovery cycle.
        existing = self.import_result.get(name)
        if existing is not None and existing.ignored != ignore:
            updated = replace(existing, ignored=ignore)
            self.import_result[name] = updated
            self._db.bus.fire(EventType.IMPORTABLE_DEVICE_ADDED, {"device": updated})

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
        cmd = [*self._esphome_cmd, "--dashboard", "config", config_path]
        await self._stream_subprocess(cmd, client, message_id)

    @api_command("devices/logs")
    async def stream_logs(
        self,
        *,
        configuration: str,
        port: str = "",
        no_states: bool = False,
        client: Any = None,
        message_id: str = "",
        **kwargs: Any,
    ) -> None:
        """
        Stream live device logs. Per-connection, not queued.

        ``no_states`` passes ``--no-states`` through to ``esphome logs``
        so component state-publish lines (sensor / binary_sensor /
        switch / cover / climate ...) are suppressed at the source.
        Mirrors the legacy dashboard's "Show entity state changes"
        toggle.
        """
        config_path = str(self._db.settings.rel_path(configuration))
        cmd = [*self._esphome_cmd, "--dashboard", "logs", config_path]
        if port:
            cmd.extend(["--device", port])
        if no_states:
            cmd.append("--no-states")
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

        ``expected_config_hash`` is read from
        ``<build_path>/build_info.json`` — ESPHome's authoritative
        post-codegen value. The metadata sidecar is consulted *only*
        as a fallback for devices whose build directory was wiped
        (clean) but where we'd previously cached a value. Reading
        from ``build_info.json`` first keeps the dashboard from
        getting stuck on a stale sidecar value if a previous run
        wrote a wrong hash (e.g. the pre-codegen subprocess hash
        the dashboard used to compute) — the next scan after this
        change picks up the canonical value automatically.
        """
        md = get_device_metadata(config_dir, filename)
        ip = str(md.get("ip", ""))
        # build_info.json wins; sidecar is the post-clean fallback.
        expected_config_hash = read_build_info_hash(config_dir / filename) or str(
            md.get("expected_config_hash", "")
        )
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
        # Eagerly probe mDNS for newly-added devices. Catches the
        # YAML-dropped-on-disk case the API entrypoints
        # (``devices/import``, ``devices/create``) can't see — e.g.
        # the user copies a config into ``config_dir`` from another
        # dashboard or git clones their setup. Without this the new
        # card sits at "Unknown" until the next periodic ping sweep
        # or mDNS announcement, even when the device is already on
        # the network. ``probe_device`` short-circuits to the
        # zeroconf cache when present; otherwise it spawns a
        # fire-and-forget resolve task.
        if kind is ScanChange.ADDED:
            self._state_monitor.probe_device(device.name)
        # The YAML cache key changed (mtime / size / inode) — clear
        # any prior failure marker so an edit gets a fresh chance at
        # ``--only-generate``. Same for REMOVED so re-creating the
        # file later doesn't inherit the old failure.
        if kind in (ScanChange.UPDATED, ScanChange.REMOVED):
            self._regenerate_failed.discard(device.configuration)
        # First-sight devices that have no compile output yet end up
        # carrying the ``<filename>.local`` address fallback and an
        # empty ``loaded_integrations`` list. Schedule a background
        # ``--only-generate`` so the next scan picks up the real
        # ``StorageJSON``-derived values without making the user wait
        # for a real compile. Same upstream pattern used in
        # ``async_schedule_storage_json_update``.
        #
        # Also fire when ``expected_config_hash`` is empty even
        # though ``loaded_integrations`` is populated. That happens
        # for devices configured before build_info.json existed (or
        # imported from an older dashboard) — they have a working
        # ``StorageJSON`` so the integrations / address / version
        # all come through, but the build directory either pre-dates
        # the build_info.json era or was wiped. Without this nudge
        # the drawer's "Local config hash" shows a permanent em-dash
        # for those devices because nothing else triggers a
        # ``--only-generate`` until the user edits the YAML.
        needs_storage_regen = kind is ScanChange.ADDED and (
            not device.loaded_integrations or not device.expected_config_hash
        )
        if needs_storage_regen:
            self._schedule_storage_regenerate(device.configuration)
        # When a configured device is deleted, re-emit cached
        # discoveries. Upstream's ``DashboardImportDiscovery`` only
        # fires ``on_update`` on first sight (``is_new`` check), so
        # without this nudge a device stays silent until it
        # re-announces — which can be many minutes for a quiet device.
        # Use the "revisit all" variant rather than matching on
        # ``device.name``: the user may have adopted with a YAML name
        # that differs from the discovered hostname (e.g. they edited
        # the MAC suffix off), in which case a name-keyed lookup
        # would miss. ``_on_import_update`` already filters configured
        # + ignored entries so re-emitting the full set is cheap and
        # only surfaces what should actually appear.
        if kind is ScanChange.REMOVED:
            self._state_monitor.revisit_all_importables()

    def _devices_by_name(self, name: str) -> list[Device]:
        """Every configured device whose ``name`` field matches ``name``.

        Two YAML files can ship the same ``name:`` value (e.g.
        ``foo.yaml`` and ``foo (1).yaml`` both pointing at
        ``foo.local``). They share a single mDNS service announcement,
        so any state / IP / version / config-hash / api-encryption
        observation needs to fan out to every matching device or the
        non-canonical copy stays stuck at "Unknown" while its sibling
        shows online. Reads the scanner's name-keyed index for an
        O(1) lookup.
        """
        return self._scanner.get_by_name(name)

    def _on_state_change(self, name: str, state: DeviceState, source: str) -> None:
        """Forward state monitor updates onto the event bus."""
        for device in self._devices_by_name(name):
            old_state = device.state
            device.state = state
            _LOGGER.info(
                "Device %s (%s): %s → %s (via %s)",
                name,
                device.configuration,
                old_state,
                state,
                source,
            )
            # Frontend's ``DeviceStateChangedEventData`` is the flat
            # ``{configuration, state}`` shape — sending the full ``device``
            # object made the destructure resolve both fields to
            # ``undefined`` and the table never updated. Match the type
            # exactly so the row's state cell flips on the next event.
            self._db.bus.fire(
                EventType.DEVICE_STATE_CHANGED,
                {"configuration": device.configuration, "state": state.value},
            )

    def _on_ip_change(self, name: str, ip: str) -> None:
        """
        Forward IP updates onto the event bus and persist non-empty values.

        ``ip=""`` means the device dropped off mDNS — we keep the
        last-known IP on disk so the OTA address cache stays warm
        across the device's offline window. The DNS pre-resolve and
        next mDNS resolve will overwrite it on reconnect.
        """
        for device in self._devices_by_name(name):
            if device.ip == ip:
                continue
            device.ip = ip
            _LOGGER.debug("Device %s (%s) IP: %s", name, device.configuration, ip or "(cleared)")
            if ip:
                self._db.create_background_task(
                    self._persist_device_ip_async(device.configuration, ip)
                )
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
        for device in self._devices_by_name(name):
            if device.deployed_version == version:
                continue

            # StorageJSON.load/save are blocking — push to a background task
            # so any error gets surfaced via the loop's exception handler.
            self._db.create_background_task(
                self._persist_storage_version_async(device.configuration, version)
            )

            old_version = device.deployed_version
            device.deployed_version = version
            device.update_available = bool(
                device.current_version and version != device.current_version
            )
            _LOGGER.info(
                "Device %s (%s) version: %s → %s (via mdns)",
                name,
                device.configuration,
                old_version or "?",
                version,
            )
            self._db.bus.fire(EventType.DEVICE_UPDATED, {"device": device})

    def _on_api_encryption_change(self, name: str, encryption: str) -> None:
        """
        Apply the API-encryption state observed via mDNS.

        Stores the broadcast value (or empty string for "TXT absent —
        device is plaintext") on the in-memory device. The dashboard's
        four-state lock indicator reads this together with
        ``api_encrypted`` to distinguish active / pending-flash /
        mismatch / plaintext.
        """
        for device in self._devices_by_name(name):
            if device.api_encryption_active == encryption:
                continue
            device.api_encryption_active = encryption
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
        for device in self._devices_by_name(name):
            if device.deployed_config_hash == config_hash:
                continue
            old_hash = device.deployed_config_hash
            device.deployed_config_hash = config_hash
            # Mtime side stays with the periodic scanner poll so this
            # callback can stay off-disk and non-blocking. A YAML edit
            # between polls (~5s window) self-corrects on the next scan.
            if device.expected_config_hash:
                device.has_pending_changes = device.expected_config_hash != config_hash
            _LOGGER.info(
                "Device %s (%s) config_hash: %s → %s (via mdns)",
                name,
                device.configuration,
                old_hash or "?",
                config_hash,
            )
            self._db.bus.fire(EventType.DEVICE_UPDATED, {"device": device})

    def _on_importable_added(self, device: AdoptableDevice) -> None:
        """Stash a newly-discovered importable device and notify subscribers."""
        # Keyed by device name so ``devices/list`` can dedupe against
        # configured devices and ``devices/ignore`` can flip the flag
        # by name without juggling the full mdns service-instance.
        self.import_result[device.name] = device
        self._db.bus.fire(EventType.IMPORTABLE_DEVICE_ADDED, {"device": device})

    def _on_importable_removed(self, name: str) -> None:
        """Forget an importable device that disappeared from mDNS."""
        if self.import_result.pop(name, None) is None:
            return
        self._db.bus.fire(EventType.IMPORTABLE_DEVICE_REMOVED, {"name": name})

    def get_importable_devices(self) -> list[AdoptableDevice]:
        """
        Snapshot of the current importable list (used for ``initial_state``).

        Filters against the configured-name set on every call so an
        adoption that landed without an mDNS Removed (the device kept
        announcing on its old name) doesn't leak through into the
        seed a fresh page load gets.
        """
        configured_names = {d.name for d in self._scanner.devices}
        return [d for d in self.import_result.values() if d.name not in configured_names]

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
        if job_type == JobType.RENAME:
            # ``esphome rename`` deletes the old YAML and writes a new
            # one with a different filename — neither path is the
            # ``configuration`` field on the job. A full scan is the
            # simplest way to pick up both the disappearance of the
            # old entry and the appearance of the new one.
            self._db.create_background_task(self._scanner.scan())
            return
        if job_type not in (JobType.COMPILE, JobType.UPLOAD, JobType.INSTALL):
            return
        configuration = getattr(job, "configuration", "")
        if not configuration:
            return
        recompute_hash = job_type in (JobType.COMPILE, JobType.INSTALL)
        flashed = job_type in (JobType.UPLOAD, JobType.INSTALL)
        self._db.create_background_task(
            self._refresh_after_firmware_job(
                configuration, recompute_hash=recompute_hash, flashed=flashed
            )
        )

    async def _refresh_after_firmware_job(
        self, configuration: str, *, recompute_hash: bool, flashed: bool
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

        When *flashed* is True (UPLOAD or INSTALL completed), the
        firmware on the device was just replaced with the binary that
        compiled to ``expected_config_hash``. The reloaded device
        otherwise keeps the *previous* mDNS-cached
        ``deployed_config_hash`` — usually a now-stale value — so the
        hash comparison reads ``expected != deployed`` and the dot
        stays orange until the rebooted device's mDNS announce
        propagates. That can be many seconds, sometimes longer if the
        device's network announce gets dropped, and the user sees a
        successful flash with a still-orange dot. Optimistically pin
        deployed = expected on the reloaded device and recompute the
        flag so the dot clears immediately. mDNS still gets to
        correct the hash later — if the new firmware advertises a
        different hash (e.g. because the OTA actually failed and the
        device kept the old image), ``_on_config_hash_change`` will
        push the real value back in.
        """
        if recompute_hash:
            await self._persist_expected_config_hash(configuration)
        await self._scanner.reload(configuration)
        if flashed:
            self._sync_deployed_hash_after_flash(configuration)

    async def _persist_expected_config_hash(self, configuration: str) -> None:
        """
        Read the canonical config_hash from build_info.json and persist it.

        ESPHome's build (and ``--only-generate``) writes the
        ``config_hash`` to ``build_info.json`` after running the full
        validate + codegen pipeline. We read that value back rather
        than recompute it, because reproducing the build's hash
        in-process is fragile — ``CORE.config_hash`` is sensitive to
        post-codegen state (id-pinning, default backfill,
        normalisation) that ``read_config`` alone doesn't apply.
        Verified against ``acfloatmonitor32.yaml``: pre-codegen yields
        ``f3e21d5a`` while the firmware bakes in ``5a94a12d``.

        No-op when the hash can't be read. The caller is on the
        post-build / post-only-generate path, so a missing or
        malformed ``build_info.json`` here is unexpected — log a
        warning so an upstream ESPHome shape change doesn't
        silently leave the sidecar out of date.
        ``compute_has_pending_changes`` will lean on the bin mtime
        in that gap, which catches the "user just edited the YAML"
        case but won't notice firmware that's drifted from the
        compile (e.g. flashed elsewhere) — the dot can read
        in-sync when it shouldn't until the next real flash
        rewrites the sidecar.
        """
        yaml_path = self._db.settings.rel_path(configuration)
        new_hash = await compute_yaml_config_hash(yaml_path)
        if not new_hash:
            _LOGGER.warning(
                "Could not read config_hash from build_info.json for %s — "
                "the drawer's Local hash may stay stale until the next flash. "
                "If this persists across compiles, check that ESPHome's "
                "build_info.json schema hasn't changed.",
                configuration,
            )
            return
        loop = asyncio.get_running_loop()
        config_dir = self._db.settings.config_dir
        await loop.run_in_executor(
            None,
            lambda: set_device_metadata(config_dir, configuration, expected_config_hash=new_hash),
        )
        _LOGGER.debug("Stored expected_config_hash for %s: %s", configuration, new_hash)

    def _sync_deployed_hash_after_flash(self, configuration: str) -> None:
        """
        Optimistically align ``deployed_config_hash`` with the just-flashed image.

        See :meth:`_refresh_after_firmware_job` for the rationale.
        Driving the update through ``apply_config_hash`` lets the
        existing ``_on_config_hash_change`` callback handle the
        device-field write + ``DEVICE_UPDATED`` event, so the
        post-flash sync follows the same code path as a real mDNS
        announce. ``apply_config_hash`` also seeds the monitor's
        per-name cache, so when the rebooted device's announce lands
        with the *same* hash the de-dup short-circuits and we don't
        fire a redundant event.
        """
        device = next(
            (d for d in self._scanner.devices if d.configuration == configuration),
            None,
        )
        if device is None or not device.expected_config_hash:
            return
        self._state_monitor.apply_config_hash(device.name, device.expected_config_hash)

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
            raw = storage_path.read_bytes()
        except FileNotFoundError:
            return
        try:
            data = loads(raw)
        except JSONDecodeError:
            # A corrupt file shouldn't tank controller bootstrap —
            # start with an empty ignored set and let the next
            # toggle_ignore call rewrite it cleanly.
            _LOGGER.warning(
                "Ignored-devices file at %s is corrupt; starting with an empty set",
                storage_path,
            )
            return
        if not isinstance(data, dict):
            _LOGGER.warning(
                "Ignored-devices file at %s isn't a JSON object; starting with an empty set",
                storage_path,
            )
            return
        ignored = data.get("ignored_devices", [])
        if not isinstance(ignored, list):
            _LOGGER.warning(
                "Ignored-devices file at %s has a non-list ``ignored_devices`` "
                "field; resetting to an empty set",
                storage_path,
            )
            self.ignored_devices = set()
            return
        self.ignored_devices = {name for name in ignored if isinstance(name, str)}

    def _save_ignored_devices(self) -> None:
        storage_path = ignored_devices_storage_path()
        storage_path.write_bytes(
            dumps_indent({"ignored_devices": sorted(self.ignored_devices)}),
        )

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
        loop = asyncio.get_running_loop()
        config_dir = self._db.settings.config_dir

        def _delete_all() -> None:
            # Existence check runs in the executor too — ``Path.exists``
            # stat()s the filesystem and would block the event loop if
            # called from the async caller.
            if not config_path.exists():
                msg = f"File not found: {configuration}"
                raise FileNotFoundError(msg)
            # Wipe the per-device PlatformIO build tree first so a partial
            # failure later in the cleanup still leaves the user able to
            # retry the delete. ``StorageJSON.build_path`` is the canonical
            # location (set during compile) — fall back to a no-op when the
            # device has never been built or the sidecar is gone.
            storage_path = ext_storage_path(configuration)
            storage = StorageJSON.load(storage_path)
            if storage is not None and storage.build_path:
                shutil.rmtree(storage.build_path, ignore_errors=True)
            config_path.unlink(missing_ok=True)
            (config_dir / ".trash" / configuration).unlink(missing_ok=True)
            (config_dir / ".archive" / f"{configuration}.json").unlink(missing_ok=True)
            try:
                storage_path.unlink(missing_ok=True)
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
            proc = await create_subprocess_exec(
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


def _apply_featured_presets(
    record: _FeaturedRecord,
    user_fields: dict[str, Any],
) -> dict[str, Any]:
    """
    Merge a featured component's presets onto *user_fields*.

    Returns a new field map ready for the regular merge logic; raises
    ``ValueError`` when *user_fields* violates a preset constraint.

    Per-field semantics:

    - Locked: user must omit the key or supply the locked value verbatim.
    - Suggestions: user-supplied value must be one of the listed values;
      omission falls back to the preset's ``value`` (when set).
    - Plain default: filled in only when the user didn't supply one.
    """
    merged: dict[str, Any] = dict(user_fields)
    for key, preset in record.featured.fields.items():
        user_value = merged.get(key)
        user_supplied = key in merged
        if preset.locked:
            # Schema validation rejects ``locked: true`` without a value, but
            # guard the runtime too so a malformed manifest fails fast with a
            # clear error instead of "locked to None".
            if preset.value is None:
                msg = (
                    f"Featured component {record.full_id} field '{key}' has "
                    f"locked=true without a value — board manifest is malformed"
                )
                raise ValueError(msg)
            if user_supplied and user_value != preset.value:
                msg = (
                    f"Featured component {record.full_id} field '{key}' is "
                    f"locked to {preset.value!r}; cannot override with "
                    f"{user_value!r}"
                )
                raise ValueError(msg)
            merged[key] = preset.value
            continue
        if preset.suggestions is not None:
            if user_supplied:
                if user_value not in preset.suggestions:
                    msg = (
                        f"Featured component {record.full_id} field '{key}' "
                        f"must be one of {preset.suggestions}; got "
                        f"{user_value!r}"
                    )
                    raise ValueError(msg)
            elif preset.value is not None:
                merged[key] = preset.value
            continue
        if not user_supplied and preset.value is not None:
            merged[key] = preset.value
    return merged


def _build_address_cache_args(device: Device, monitor: DeviceStateMonitor | None) -> list[str]:
    """Build CLI cache args from the IPs we already have for *device*."""
    address = device.address
    if not address:
        return []

    # mDNS hostnames are case-insensitive and may carry a trailing dot;
    # normalise once so the CLI cache key matches what it'll look up.
    normalized = normalize_hostname(address)
    is_local = is_local_hostname(address)

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
