"""
Devices controller — device CRUD, file watching, CLI operations, state management.

WS command surface plus the supporting state-monitor / scanner /
MQTT-coordinator glue. Pure data and free helpers live in
``constants.py`` and ``helpers.py``; the class itself lives here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from esphome.core import CORE
from esphome.helpers import write_file as atomic_write_file
from esphome.zeroconf import AsyncEsphomeZeroconf

from ...constants import is_secrets_file
from ...helpers.api import CommandError, api_command
from ...helpers.async_ import run_in_executor
from ...helpers.build_size import BuildSizeRefreshResult
from ...helpers.device_yaml import (
    board_requires_wifi,
    configuration_stem,
)
from ...helpers.event_bus import Event
from ...helpers.secrets_state import (
    SecretsContentError,
    read_secrets_yaml,
    validate_secrets_content,
    validate_wifi_credentials,
    wifi_secrets_defined,
    write_wifi_secrets,
)
from ...helpers.storage import ShutdownCallback
from ...models import (
    AddComponentResponse,
    Device,
    DeviceEventData,
    DeviceReachabilityData,
    DevicesResponse,
    DeviceState,
    ErrorCode,
    EventType,
    ImportBundleResponse,
    JobLifecycleData,
    ReachabilitySource,
    UpdateDeviceResponse,
    WizardResponse,
)
from .._build_size_refresher import BuildSizeRefresher
from .._device_mqtt_coordinator import DeviceMqttCoordinator
from .._device_scanner import DeviceScanner, ScanChange
from .._device_state_monitor import DeviceStateMonitor
from .._reachability_tracker import ReachabilityTracker
from ..firmware.helpers import _find_esphome_cmd
from ..version_history import GIT_COMMIT_ERRORS
from . import (
    add_component,
    api_key,
    archive,
    firmware_sync,
    importable,
    logs,
    mutations_clone,
    mutations_create,
    mutations_import_bundle,
    mutations_simple,
    mutations_yaml,
    reachability,
    scan_change,
    search,
    state_callbacks,
    storage_regen,
    validate,
)
from ._metadata_store import DeviceMetadataStore
from ._shared_sidecar import SharedSidecarClient
from ._state import DevicesState
from ._yaml_search_cache import YamlSearchCache
from .helpers import (
    _build_address_cache_args,
    _validate_archive_configuration,
)
from .metadata import DeviceMetadataBase

if TYPE_CHECKING:
    from ...device_builder import DeviceBuilder
    from ...models import AdoptableDevice, BoardCatalogEntry

_LOGGER = logging.getLogger(__name__)

# How long the persisted "regen failed" stamp is honoured before a
# restart-time check is allowed to re-spawn ``--only-generate`` for
# the same untouched YAML. The in-memory ``_regenerate_failed`` set
# blocks within a session until the user edits the YAML; the TTL
# only applies cross-restart, so a transient external problem
# (git package server flaky, DNS hiccup) eventually recovers
# without forcing the user to touch the file. One hour is short
# enough that "I'll come back to this in a bit and restart" works,
# long enough that a debugger restarting the dashboard 10x in a
# row doesn't churn through 10 spawns on the same broken config.
_REGEN_FAILURE_TTL_SECONDS: float = 3600.0


class DevicesController(  # noqa: PLR0904 (grandfathered; new public methods need a refactor first)
    DeviceMetadataBase,
):
    """Manage device configurations, file watching, and CLI operations."""

    def __init__(self, device_builder: DeviceBuilder) -> None:
        super().__init__(device_builder)
        self.state = DevicesState()
        # Unsubscribe handle for the firmware-job-completion listener
        # wired up in start(); held so stop() can detach cleanly.
        self._unsub_job_completed: Any = None
        # Guards poll() from re-arming a torn-down scanner during the shutdown drain.
        self._stopped = False
        # Pending post-flash version re-probe timers, keyed on
        # configuration so a re-flash cancels its predecessor; cancelled
        # en masse in stop().
        self._reprobe_timers: dict[str, asyncio.TimerHandle] = {}

        # Constructed before the scanner so the first
        # ``_resolve_device_metadata`` reads off the store.
        self._shutdown_callbacks: list[ShutdownCallback] = []
        self._metadata_store = DeviceMetadataStore(
            config_dir=self._db.settings.config_dir,
            data_dir=Path(CORE.data_dir),
            shutdown_register=self._shutdown_callbacks.append,
        )
        self._shared_sidecar = SharedSidecarClient(self._db.settings.config_dir)

        # Per-file locks serialising a YAML write with its version-history
        # commit (see ``_persist_yaml_mutation``). One ``asyncio.Lock`` per
        # distinct filename written this process lifetime; never evicted —
        # popping on delete could desync a lock a concurrent save is
        # awaiting. Negligible in practice (a real fleet reuses filenames);
        # only churn through many unique names grows it.
        self._yaml_write_locks: dict[str, asyncio.Lock] = {}

        # Background ``--only-generate`` bookkeeping. ``--only-generate``
        # validates a YAML and writes its ``StorageJSON`` without doing
        # a real build; we trigger it whenever a YAML is saved or
        # first-seen with no compile output. Three guards stop us from
        # spinning:
        #   * ``state.regenerate_pending`` — configurations already in
        #     flight (scheduled but not yet finished). Skip duplicate
        #     schedules.
        #   * ``state.regenerate_failed`` — YAMLs whose last attempt
        #     failed. Don't retry until the file changes (cleared on
        #     ``ScanChange.UPDATED``).
        #   * ``_regenerate_lock`` — serialises the actual subprocess
        #     so we don't spawn N esphome compiles in parallel.
        self._regenerate_lock = asyncio.Lock()

        # ``yaml/search`` per-file cache. The class owns its own
        # ``stat``-then-read flow + ``asyncio.Lock`` so the
        # bookkeeping doesn't sprawl across this controller. See
        # ``_yaml_search_cache.YamlSearchCache``.
        self._yaml_search_cache = YamlSearchCache()
        # Global search lock — ``yaml/search`` is I/O-bound (one
        # ``stat`` per device + reads on cache misses), so two
        # concurrent searches against the same fleet would just
        # double the disk pressure without helping latency. Serialise
        # to one in-flight call per controller; the frontend's
        # debounce + concurrency-of-1 gate keeps the queue depth low
        # in normal use, and a slow request from a stuck client
        # won't fan out to N parallel walks.
        self._yaml_search_lock = asyncio.Lock()

        self._scanner = DeviceScanner(
            config_dir=self._db.settings.config_dir,
            get_metadata=self._resolve_device_metadata,
            on_change=self._on_scan_change,
        )
        # Single-worker build-size refresher. Bulk operations
        # (clean / delete N devices in a row, fleet-wide startup
        # sweep) all funnel into one queue so repeated requests
        # for the same configuration coalesce and we never pile
        # up background tasks.
        self._build_size = BuildSizeRefresher(
            get_filenames=lambda: (d.configuration for d in self._get_devices()),
            get_metadata_snapshot=self._metadata_store.snapshot_all,
            persist_size=self._persist_build_size,
            on_refreshed=self._scanner.reload,
        )
        # Build the state monitor first so the reachability tracker
        # can take its ``get_mdns_cache_info`` bound method directly
        # as the mDNS cache reader (no wrapper lambda — bound
        # methods already match the ``Callable[[str], MdnsCacheInfo
        # | None]`` shape). Wire the tracker back onto the monitor
        # after construction; the monitor only invokes
        # ``self._reachability`` at observation time so the
        # initial ``None`` is fine.
        self._state_monitor = DeviceStateMonitor(
            get_devices=self._get_devices,
            get_devices_by_name=self._scanner.get_by_name,
            on_state_change=self._on_state_change,
            on_ip_change=self._on_ip_change,
            on_source_change=self._on_source_change,
            on_version_change=self._on_version_change,
            on_config_hash_change=self._on_config_hash_change,
            on_api_encryption_change=self._on_api_encryption_change,
            on_mac_address_change=self._on_mac_address_change,
            on_importable_added=self._on_importable_added,
            on_importable_removed=self._on_importable_removed,
            is_ignored=self.state.ignored_devices.__contains__,
            presence=self._db.subscriber_presence,
            resolve_api_connection=self._resolve_device_api_connection,
        )
        # Per-signal freshness tracker (mDNS / ping / MQTT last-seen,
        # ping RTT) feeding the device drawer's Reachability section.
        # Lives here on the controller so the subscribe handler can
        # call ``snapshot()`` on demand; observations come in via the
        # state monitor.
        self._reachability = ReachabilityTracker(
            on_observation=self._on_reachability_observation,
            mdns_cache_reader=self._state_monitor.get_mdns_cache_info,
        )
        self._state_monitor.set_reachability(self._reachability)
        # MQTT routes its observations through the same state monitor so
        # source-priority is enforced in one place.
        self._mqtt_coordinator = DeviceMqttCoordinator(
            config_dir=self._db.settings.config_dir,
            get_devices=self._get_devices,
            on_state_change=lambda n, s: self._state_monitor.apply(n, s, "mqtt"),
            on_ip_change=self._state_monitor.apply_ip,
        )

    @property
    def zeroconf(self) -> AsyncEsphomeZeroconf | None:
        """
        The mDNS responder owned by the state monitor, or ``None``.

        Surfaced so the dashboard's own ``_esphomebuilder._tcp.local.``
        advertiser can reuse the existing instance instead of standing
        up a second responder. ``None`` when zeroconf failed to start —
        callers skip their advertise.
        """
        return self._state_monitor.zeroconf

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialise — load state, scan files, start mDNS + ping + MQTT discovery."""
        self._stopped = False
        self.state.esphome_cmd = _find_esphome_cmd()
        # Seed the store (and migrate on first post-upgrade boot)
        # before the scanner runs — resolver reads off it.
        await self._metadata_store.async_load()
        await self.migrate_board_id_user_set()
        await run_in_executor(self._load_ignored_devices)
        await self._scanner.scan()
        self._scanner.start()
        _LOGGER.info("Devices controller started — %d devices loaded", len(self._scanner.devices))
        await self._state_monitor.start()
        await self._mqtt_coordinator.reconcile()
        self._unsub_job_completed = self._db.bus.add_listener(
            EventType.JOB_COMPLETED, self._on_firmware_job_completed
        )
        # Build-size worker — runs its own initial fleet sweep
        # on first iteration to pick up CLI-compile drift, then
        # drains per-device requests as they arrive from the
        # job-completion hook.
        self._build_size.start()

    async def stop(self) -> None:
        """Stop background monitors so the process exits cleanly."""
        self._stopped = True
        if self._unsub_job_completed is not None:
            self._unsub_job_completed()
            self._unsub_job_completed = None
        self._cancel_reprobe_timers()
        await self._scanner.stop()
        await self._build_size.stop()
        await self._mqtt_coordinator.stop()
        await self._state_monitor.stop()
        for callback in self._shutdown_callbacks:
            await callback()

    async def poll(self) -> None:
        """Poll for file changes; a no-op once stopped (don't re-arm during shutdown)."""
        if self._stopped:
            return
        await self._scanner.scan()
        await self._mqtt_coordinator.reconcile()

    def get_devices(self) -> list[Device]:
        """Snapshot of the currently-loaded devices."""
        return self._scanner.devices

    def get_by_configuration(self, configuration: str) -> Device | None:
        """Return the configured device for YAML filename *configuration*, or ``None``."""
        return self._scanner.get_by_configuration(configuration)

    async def reload_configuration(self, filename: str) -> bool:
        """
        Force-reload one device's state from disk and the metadata sidecar.

        Use after writing a sidecar field whose value isn't reflected
        in the YAML's mtime (labels, IP cache after restart-driven
        re-resolution, etc.) — the scanner's mtime-based cache would
        otherwise skip the file. Fires ``DEVICE_UPDATED`` via the
        scanner's existing scan-change pipeline. Returns ``True``
        when the device exists and was reloaded.
        """
        return await self._scanner.reload(filename)

    def get_address_cache_args(self, configuration: str) -> list[str]:
        """
        Return ``--mdns/--dns-address-cache`` CLI args for *configuration*.

        Empty list when the device is unknown, has no OTA-capable
        integration loaded, or has no cached IP available.
        """
        target_name = configuration_stem(configuration)
        device = next((d for d in self._scanner.devices if d.name == target_name), None)
        if device is None:
            return []
        # The CLI only consults the address cache from upload paths
        # that resolve via ``CORE.address_cache``. That used to be just
        # the Native API OTA client (``espota2``), but esphome/esphome#16207
        # added an HTTP OTA path through the ``web_server`` component
        # that goes through the same resolver. Either integration is
        # enough for the cache to be useful — passing the args to a
        # build that doesn't read them is harmless. Devices loading
        # neither (e.g. MQTT-only configs) flash via paths that don't
        # take a host/port at all, so the cache args are noise there.
        loaded = device.loaded_integrations
        if "api" not in loaded and "web_server" not in loaded:
            return []
        return _build_address_cache_args(device, self._state_monitor)

    def get_ota_address_cache_args(self, configuration: str, port: str | None) -> list[str]:
        """Return cache args when ``port == "OTA"`` (or ``None`` for always-OTA flows)."""
        if port is not None and port != "OTA":
            return []
        return self.get_address_cache_args(configuration)

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
        importable = [
            d for d in self.state.import_result.values() if d.name not in configured_names
        ]
        return DevicesResponse(configured=configured, importable=importable)

    @api_command("devices/get_states")
    async def get_device_states(self, **kwargs: Any) -> dict:
        """Get connectivity state for all devices."""
        return {d.configuration: d.state.value for d in self._scanner.devices}

    @api_command("yaml/search")
    async def search_yaml(
        self,
        *,
        query: str,
        max_results: int = 50,
        case_sensitive: bool = False,
        context_lines: int | None = None,
        **kwargs: Any,
    ) -> list[dict]:
        """Substring-search every configured device's raw YAML file."""
        return await search.search_yaml(
            self,
            query=query,
            max_results=max_results,
            case_sensitive=case_sensitive,
            context_lines=context_lines,
        )

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
        overwrite: bool = False,
        **kwargs: Any,
    ) -> WizardResponse:
        """Create a new device configuration."""
        return await mutations_create.create_device(
            self,
            name=name,
            board_id=board_id,
            ssid=ssid,
            psk=psk,
            file_content=file_content,
            overwrite=overwrite,
        )

    @api_command("devices/import_bundle")
    async def import_bundle(
        self,
        *,
        file_content_b64: str,
        overwrite: list[str] | None = None,
        **kwargs: Any,
    ) -> ImportBundleResponse:
        """Import an ``esphome bundle`` archive as a device."""
        return await mutations_import_bundle.import_bundle(
            self,
            file_content_b64=file_content_b64,
            overwrite=overwrite,
        )

    @api_command("devices/update")
    async def update_device(
        self,
        *,
        configuration: str,
        friendly_name: str | None = None,
        comment: str | None = None,
        board_id: str | None = None,
        **kwargs: Any,
    ) -> UpdateDeviceResponse:
        """Update device metadata (sidecar JSON, not the YAML file)."""
        return await mutations_simple.update_device(
            self,
            configuration=configuration,
            friendly_name=friendly_name,
            comment=comment,
            board_id=board_id,
        )

    @api_command("devices/set_labels")
    async def set_labels(
        self,
        *,
        configuration: str,
        label_ids: list[str],
        **kwargs: Any,
    ) -> Device:
        """Replace this device's label assignments."""
        return await mutations_simple.set_labels(
            self, configuration=configuration, label_ids=label_ids
        )

    @api_command("devices/set_labels_bulk")
    async def set_labels_bulk(
        self, *, updates: list[dict[str, Any]], **kwargs: Any
    ) -> list[dict[str, Any]]:
        """Assign labels across multiple devices."""
        return await mutations_simple.set_labels_bulk(self, updates=updates)

    @api_command("devices/rename")
    async def rename_device(
        self,
        *,
        configuration: str,
        new_name: str,
        config_only: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        Rename a device configuration.

        Default path delegates to ``esphome rename`` (compile + OTA).
        ``config_only`` rewrites the YAML + ``esphome.name`` without
        flashing; the caller uses it after the user confirms renaming
        an offline device.
        """
        return await mutations_simple.rename_device(
            self, configuration=configuration, new_name=new_name, config_only=config_only
        )

    @api_command("devices/clone")
    async def clone_device(
        self,
        *,
        configuration: str,
        new_name: str,
        new_friendly_name: str | None = None,
        **kwargs: Any,
    ) -> dict[str, str]:
        """Duplicate an existing device YAML under a fresh hostname."""
        return await mutations_clone.clone_device(
            self,
            configuration=configuration,
            new_name=new_name,
            new_friendly_name=new_friendly_name,
        )

    @api_command("devices/edit_friendly_name")
    async def edit_friendly_name(
        self,
        *,
        configuration: str,
        new_friendly_name: str,
        **kwargs: Any,
    ) -> dict[str, str | bool]:
        """Rewrite ``esphome.friendly_name:`` in the device YAML."""
        return await mutations_simple.edit_friendly_name(
            self,
            configuration=configuration,
            new_friendly_name=new_friendly_name,
        )

    async def _yaml_content_for_create(
        self,
        name: str,
        friendly: str,
        board: BoardCatalogEntry | None,
        file_content: str | None,
        ssid: str,
        psk: str,
    ) -> tuple[str, mutations_yaml.CreateYamlSource]:
        """
        Build the YAML body for ``devices/create``, resolving Wi-Fi.

        Provided credentials are persisted to ``secrets.yaml`` (validated)
        and the config emits ``!secret`` — bare credentials are never
        written into the device YAML. A supplied *ssid* is an explicit "use
        Wi-Fi" intent, so it keeps the ``wifi:`` block even on an
        onboard-network board (no Ethernet auto-pull). An empty *ssid* reuses
        whatever ``secrets.yaml`` already holds and lets a networked board
        default to Ethernet (or a no-Wi-Fi board fall to the no-network stub).
        """
        wifi_secrets_available = True
        # A supplied ssid means "this device uses Wi-Fi": keep the wifi block
        # even on a board that would otherwise auto-pull onboard Ethernet.
        wifi_requested = False
        if file_content:
            pass  # user YAML written as-is; ssid/psk ignored
        elif ssid:
            # Persist the user's Wi-Fi to secrets.yaml and emit !secret rather
            # than inlining bare credentials into the device config. The write
            # must land *before* the caller validates the generated YAML —
            # validation runs ``esphome config``, which resolves
            # ``!secret wifi_ssid`` against the on-disk file. A later
            # generate/validate failure therefore leaves the secret persisted;
            # that's benign — ``wifi_ssid`` / ``wifi_password`` is a shared,
            # idempotent upsert (identical to ``config/set_wifi_credentials``)
            # that the next device reuses, not per-device state.
            try:
                validate_wifi_credentials(ssid, psk)
            except SecretsContentError as err:
                raise CommandError(ErrorCode.INVALID_ARGS, str(err)) from err
            await self._db.write_secrets_locked(
                write_wifi_secrets, self._db.settings.config_dir, ssid, psk
            )
            ssid, psk = "", ""  # force the !secret path in the generator
            wifi_requested = True
        else:
            secrets = await run_in_executor(read_secrets_yaml, self._db.settings.config_dir)
            wifi_secrets_available = wifi_secrets_defined(secrets)
            # A Wi-Fi-only board with no secrets would generate an unflashable
            # no-network stub (its api/ota/web_server defaults need a network).
            # Refuse cleanly instead of letting it surface as a generator bug.
            if not wifi_secrets_available and board is not None and board_requires_wifi(board):
                raise CommandError(
                    ErrorCode.INVALID_ARGS,
                    "This board connects over Wi-Fi; provide an SSID or set "
                    "Wi-Fi credentials first.",
                )
        return await mutations_yaml.yaml_content_for_create(
            name,
            friendly,
            board,
            file_content,
            ssid,
            psk,
            wifi_secrets_available=wifi_secrets_available,
            wifi_requested=wifi_requested,
            catalog=self._db.components,
        )

    async def _validate_rewritten_yaml_or_raise(
        self,
        configuration: str,
        content: str,
        *,
        action: str,
        on_failure: ErrorCode = ErrorCode.INVALID_ARGS,
        on_error_cleanup: Callable[[], None] | None = None,
        tolerate_unavailable: bool = False,
        timeout: float | None = None,
    ) -> None:
        await mutations_yaml.validate_rewritten_yaml_or_raise(
            self._db.editor,
            configuration,
            content,
            action=action,
            on_failure=on_failure,
            on_error_cleanup=on_error_cleanup,
            tolerate_unavailable=tolerate_unavailable,
            timeout=timeout,
        )

    @api_command("devices/delete")
    async def delete_device(self, *, configuration: str, **kwargs: Any) -> None:
        """Delete a device and all associated files."""
        await self._delete_single(configuration)
        await self._scanner.scan()

    @api_command("devices/archive")
    async def archive_device(self, *, configuration: str, **kwargs: Any) -> None:
        """Soft-delete a device — keep the YAML, wipe build artifacts.

        Moves the YAML to ``<config_dir>/archive/`` so the user
        can ``unarchive`` later. Build dir + StorageJSON sidecar
        are wiped (build artifacts go stale on archive). The
        device-metadata sidecar's volatile fields (``ip``,
        ``expected_config_hash``) are cleared but its stable
        identity fields (``board_id``, ``friendly_name``,
        ``comment``) survive so an unarchive of the same YAML
        restores the user-visible state unchanged — ``board_id``
        is the catalog → YAML match key. See ``_archive_single``
        for the full keep / clear rationale.
        """
        _validate_archive_configuration(configuration)
        try:
            await self._archive_single(configuration)
        except FileNotFoundError as exc:
            raise CommandError(ErrorCode.NOT_FOUND, str(exc)) from exc
        await self._scanner.scan()

    @api_command("devices/unarchive")
    async def unarchive_device(self, *, configuration: str, **kwargs: Any) -> None:
        """Restore an archived device's YAML to the configured config_dir.

        The scanner's next sweep picks the file up and fires
        ``DEVICE_ADDED`` so the dashboard's active list refreshes
        without a manual reload.
        """
        _validate_archive_configuration(configuration)
        try:
            await self._unarchive_single(configuration)
        except FileNotFoundError as exc:
            raise CommandError(ErrorCode.NOT_FOUND, str(exc)) from exc
        await self._scanner.scan()

    @api_command("devices/list_archived")
    async def list_archived(self, **kwargs: Any) -> list[dict[str, Any]]:
        """List archived devices with their parsed name / friendly_name / comment.

        Read-only — surfaces the contents of
        ``<config_dir>/archive/`` for the dashboard's "Show
        archived devices" toggle. Each entry carries enough info
        for the UI to render a row + Unarchive / Delete-permanently
        actions; full YAML / metadata is left on disk and is fetched
        on demand if the user opens one.
        """
        return await run_in_executor(self._list_archived_sync)

    @api_command("devices/delete_archived")
    async def delete_archived(self, *, configuration: str, **kwargs: Any) -> None:
        """Permanently delete an archived device's YAML.

        The companion to ``archive`` for the case where the user
        decided they really don't want this device back. Removes
        ``<config_dir>/archive/<configuration>``. The StorageJSON
        sidecar and device-metadata entry are usually already gone
        (``archive`` wipes them on the way in); this command also
        cleans up any orphan sidecars left over from legacy /
        pre-existing archives, but skips that cleanup if an active
        config of the same filename exists (its sidecars belong to
        the live device). Surfaces ``CommandError(NOT_FOUND)``
        when the archive entry is gone — symmetric with
        ``unarchive``.
        """
        _validate_archive_configuration(configuration)
        try:
            await self._delete_archived_single(configuration)
        except FileNotFoundError as exc:
            raise CommandError(ErrorCode.NOT_FOUND, str(exc)) from exc

    @api_command("devices/delete_bulk")
    async def delete_bulk(
        self, *, configurations: list[str], **kwargs: Any
    ) -> list[dict[str, Any]]:
        """
        Delete multiple devices at once.

        Returns one ``{configuration, success, error?}`` dict per device.
        """
        return await self._run_bulk_per_device(configurations, self._delete_single)

    @api_command("devices/archive_bulk")
    async def archive_bulk(
        self, *, configurations: list[str], **kwargs: Any
    ) -> list[dict[str, Any]]:
        """
        Archive multiple devices at once.

        Returns one ``{configuration, success, error?}`` dict per device.
        Mirrors ``delete_bulk`` so the frontend's bulk-archive flow can
        consume a single per-device result list instead of fanning out
        N separate ``devices/archive`` calls.
        """

        async def _archive(configuration: str) -> None:
            _validate_archive_configuration(configuration)
            await self._archive_single(configuration)

        return await self._run_bulk_per_device(configurations, _archive)

    async def _run_bulk_per_device(
        self,
        configurations: list[str],
        action: Callable[[str], Awaitable[None]],
    ) -> list[dict[str, Any]]:
        return await archive.run_bulk_per_device(self, configurations, action)

    @api_command("devices/get_config")
    async def get_config(self, *, configuration: str, **kwargs: Any) -> str:
        """Read device config YAML; a missing file is NOT_FOUND, not internal_error."""
        try:
            return await self._read_yaml_async(self._db.settings.rel_path(configuration))
        except FileNotFoundError as err:
            raise CommandError(ErrorCode.NOT_FOUND, f"Device {configuration!r} not found") from err

    @api_command("devices/update_config")
    async def update_config(
        self, *, configuration: str, content: str, allow_wipe: bool = False, **kwargs: Any
    ) -> None:
        """
        Write device config YAML.

        ``allow_wipe`` permits clearing secrets.yaml to empty; without it an
        empty secrets save is refused. An empty device YAML is always refused.
        """
        if not isinstance(allow_wipe, bool):
            raise CommandError(ErrorCode.INVALID_ARGS, "allow_wipe must be a boolean")
        is_empty = not content.strip()
        if is_secrets_file(configuration):
            if is_empty and not allow_wipe:
                raise CommandError(
                    ErrorCode.INVALID_ARGS,
                    "refusing to clear all secrets from secrets.yaml without "
                    "confirmation; pass allow_wipe to confirm",
                )
            try:
                validate_secrets_content(content, self._db.settings.rel_path(configuration))
            except SecretsContentError as err:
                raise CommandError(
                    ErrorCode.INVALID_ARGS,
                    f"refusing to save invalid secrets.yaml: {err}",
                ) from err
            # Hold the shared lock so a whole-file save can't interleave with a
            # per-key config/set_secret. A full save still replaces the document
            # (last-write-wins by design); the lock only prevents torn writes.
            async with self._db.secrets_write_lock:
                await self._persist_yaml_mutation(
                    configuration, content, message=f"Edit {configuration}"
                )
            return
        if is_empty:
            raise CommandError(
                ErrorCode.INVALID_ARGS,
                f"refusing to write empty content to {configuration!r} to prevent "
                "accidental data loss; use the delete action to remove a file",
            )
        await self._persist_yaml_mutation(configuration, content, message=f"Edit {configuration}")

    async def apply_restored_yaml(
        self, configuration: str, content: str, *, restored_from: str
    ) -> None:
        """Write a version-history-restored YAML back to disk; recreates a deleted file."""
        await self._persist_yaml_mutation(
            configuration, content, message=f"Restore {configuration} to {restored_from}"
        )

    def _schedule_storage_regenerate(self, configuration: str) -> None:
        storage_regen.schedule(self, configuration)

    async def _spawn_only_generate(self, configuration: str) -> bool:
        return await storage_regen.spawn_only_generate(self, configuration)

    async def _regen_already_failed_recently_async(self, configuration: str) -> bool:
        return await storage_regen.already_failed_recently_async(self, configuration)

    async def _stamp_regen_failure(self, configuration: str) -> None:
        await storage_regen.stamp_failure(self, configuration)

    async def _finalize_regen_success(self, configuration: str) -> None:
        await storage_regen.finalize_success(self, configuration)

    @api_command("devices/get_api_key")
    async def get_api_key(self, *, configuration: str, **kwargs: Any) -> dict[str, str]:
        """Return the resolved Native API encryption key for *configuration*."""
        return await api_key.get_api_key(self, configuration)

    async def _resolve_api_key_via_esphome_config(self, configuration: str) -> str:
        return await api_key.resolve_via_esphome_config(self, configuration)

    async def _resolve_device_api_connection(self, configuration: str) -> tuple[str, int]:
        """Native API (encryption key, port) for the state monitor's API info fallback."""
        return await api_key.get_api_connection(self, configuration)

    @api_command("devices/add_component")
    async def add_component(
        self,
        *,
        configuration: str,
        component_id: str,
        fields: dict[str, Any] | None = None,
        yaml: str | None = None,
        **kwargs: Any,
    ) -> AddComponentResponse:
        """Add a component block to a device YAML.

        Pass ``yaml`` to merge into the caller's unsaved editor draft and
        return the result without writing disk; the editor saves it later.
        Omit it to merge into the on-disk YAML and persist.
        """
        return await add_component.add_component(
            self,
            configuration=configuration,
            component_id=component_id,
            fields=fields,
            yaml=yaml,
        )

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
        return await importable.import_device(
            self,
            name=name,
            project_name=project_name,
            package_import_url=package_import_url,
            friendly_name=friendly_name,
            encryption=encryption,
        )

    @api_command("devices/ignore")
    async def toggle_ignore(self, *, name: str, ignore: bool = True, **kwargs: Any) -> None:
        """Mark a discovered device as ignored / visible in the import list."""
        await importable.toggle_ignore(self, name=name, ignore=ignore)

    # ------------------------------------------------------------------
    # API commands — per-connection streams (validate, logs)
    # ------------------------------------------------------------------

    @api_command("devices/validate")
    async def validate_config(
        self,
        *,
        configuration: str,
        show_secrets: bool = False,
        client: Any = None,
        message_id: str = "",
        **kwargs: Any,
    ) -> None:
        """Validate a device YAML config; streams output per-connection."""
        await validate.validate_config(
            self,
            configuration=configuration,
            show_secrets=show_secrets,
            client=client,
            message_id=message_id,
        )

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
        """Stream live device logs. Per-connection, not queued."""
        await logs.stream_logs(
            self,
            configuration=configuration,
            port=port,
            no_states=no_states,
            client=client,
            message_id=message_id,
        )

    @api_command("devices/stop_stream")
    async def stop_stream(
        self,
        *,
        stream_id: str,
        client: Any = None,
        **kwargs: Any,
    ) -> dict:
        """Cancel a streaming command on this connection."""
        return logs.stop_stream(client, stream_id)

    @api_command("devices/subscribe_reachability")
    async def subscribe_reachability(
        self,
        *,
        device_name: str,
        client: Any = None,
        message_id: str = "",
        **kwargs: Any,
    ) -> None:
        """
        Stream per-signal reachability for a single device.

        Drawer-only: while the device drawer is open the frontend
        opens this stream so it can show "mDNS heard 12s ago, ping
        47s ago, MQTT 2 min ago, RTT 4 ms" without bloating the
        broadcast ``subscribe_events`` channel for every other
        connected client. Pair with ``devices/stop_stream`` (or a
        WS disconnect) to unsubscribe.
        """
        await reachability.subscribe(
            self, device_name=device_name, client=client, message_id=message_id
        )

    async def _reachability_refresh_loop(self, device_name: str) -> None:
        await reachability.refresh_loop(self, device_name)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_devices(self) -> list[Device]:
        """Bridge for the state monitor (``self._scanner.devices`` is a property)."""
        return self._scanner.devices

    def _fire_device_updated(self, device: Device) -> None:
        """Broadcast ``DEVICE_UPDATED`` for *device* on the event bus."""
        self._db.bus.fire(EventType.DEVICE_UPDATED, DeviceEventData(device=device))

    @staticmethod
    async def _write_yaml_atomic_async(path: Path, content: str) -> None:
        """Atomically write *content* to *path* off the executor.

        Use this for any user-editable YAML write so a mid-write
        crash can't leave the file empty or half-written;
        ``Path.write_text`` truncates before writing and isn't
        safe for those paths.
        """
        await run_in_executor(atomic_write_file, path, content)

    async def _persist_yaml_mutation(
        self, configuration: str, content: str, *, message: str | None = None
    ) -> None:
        """
        Atomically write *content*, commit it to history, then reload.

        The write and the inline version-history commit are serialised
        per file: ``commit_paths`` stages whatever is on disk, so a
        concurrent writer to the same *configuration* must not slip
        between this write and its commit and win the history slot. The
        commit is awaited (not fire-and-forget) so the content is
        captured before the next save can overwrite it.
        """
        async with self._yaml_write_lock(configuration):
            await self._write_yaml_atomic_async(self._db.settings.rel_path(configuration), content)
            await self._commit_history(configuration, message or f"Update {configuration}")
        # A write here (device YAML, or the whole-file secrets.yaml editor)
        # can change what any open editor's lint resolves; clear the caches
        # so the next validate re-reads disk instead of the stale result.
        self._db.invalidate_editor_cache()
        self._scanner.request(configuration)
        # Mirrors the upstream dashboard's
        # ``async_schedule_storage_json_update``; without it
        # ``loaded_integrations`` stays at its pre-write state.
        self._schedule_storage_regenerate(configuration)

    def _yaml_write_lock(self, configuration: str) -> asyncio.Lock:
        """Return the per-file lock guarding a YAML write + its history commit."""
        lock = self._yaml_write_locks.get(configuration)
        if lock is None:
            lock = self._yaml_write_locks[configuration] = asyncio.Lock()
        return lock

    async def _commit_history(self, configuration: str, message: str) -> None:
        """
        Record *configuration* in version history; swallow genuine git errors.

        A git/subprocess failure keeps a hiccup from breaking the user's
        save (recoverable history gap for this one save); a programming
        bug propagates rather than being mislabelled as a git failure.
        Does **not** itself take the per-file ``_yaml_write_lock``; callers
        needing write+commit atomicity (editor save, delete, archive) hold
        it across this call so same-config commits can't interleave.
        """
        version_history = self._db.version_history
        if version_history is None:
            return
        try:
            await version_history.record_configuration(configuration, message)
        except GIT_COMMIT_ERRORS:
            # Leave any queued catch-all entry in place so the debounced
            # flush still records this save's content (generic message).
            _LOGGER.exception("Version-history commit failed for %s", configuration)
            return
        # Committed with the rich message; drop a now-redundant queued
        # catch-all entry so its generic message can't supersede it.
        version_history.discard_pending(configuration)

    async def _register_new_device(
        self,
        configuration: str,
        commit_message: str,
        *,
        board_id: str | None = None,
        clear_metadata: bool = True,
    ) -> None:
        """
        Make a freshly written config visible: reset metadata, commit, scan.

        Shared tail of ``create_device`` and ``import_bundle``. For a new
        device, clearing metadata first stops an archived board_id from
        mis-binding to a fresh device reusing the same filename. An
        *overwrite* of an existing device passes ``clear_metadata=False``
        so its labels / comment / board_id survive. *board_id* is persisted
        only when explicitly chosen. The scan fires ``_on_scan_change``
        (ADDED), which probes the device, so callers must not double-probe.
        """
        if clear_metadata:
            await self._delete_device_metadata(configuration)
        if board_id:
            await self._persist_device_metadata_async(
                configuration, board_id=board_id, board_id_user_set=True
            )
        await self._commit_history(configuration, commit_message)
        await self._scanner.scan()

    @staticmethod
    async def _read_yaml_async(path: Path) -> str:
        """Read *path* as UTF-8 text off the executor."""
        return await run_in_executor(path.read_text, "utf-8")

    def _on_scan_change(self, kind: ScanChange, device: Device) -> None:
        scan_change.on_scan_change(self, kind, device)

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

    def _build_reachability_snapshot(self, name: str) -> DeviceReachabilityData | None:
        return reachability.build_snapshot(self, name)

    def _on_reachability_observation(self, name: str) -> None:
        reachability.on_observation(self, name)

    def get_reachability_snapshot(self, name: str) -> DeviceReachabilityData | None:
        """Return the current reachability snapshot for *name*, or ``None``.

        Public so the WS ``devices/subscribe_reachability`` handler can
        seed its initial event without going through the bus. Returns
        ``None`` when no configured device matches *name* (the
        subscription handler maps that to a NOT_FOUND error).
        """
        return reachability.build_snapshot(self, name)

    async def refresh_device_mdns(self, name: str) -> None:
        """Force-refresh a device's mDNS A record. No-op if zeroconf is down."""
        await reachability.refresh_device_mdns(self, name)

    def _on_state_change(self, name: str, state: DeviceState, source: str) -> None:
        state_callbacks.on_state_change(self, name, state, source)

    def _on_ip_change(self, name: str, ip: str, addresses: list[str]) -> None:
        state_callbacks.on_ip_change(self, name, ip, addresses)

    def _on_source_change(self, name: str, source: ReachabilitySource) -> None:
        state_callbacks.on_source_change(self, name, source)

    def _on_version_change(self, name: str, version: str) -> None:
        state_callbacks.on_version_change(self, name, version)

    def _on_mac_address_change(self, name: str, mac: str) -> None:
        state_callbacks.on_mac_address_change(self, name, mac)

    def _on_api_encryption_change(self, name: str, encryption: str) -> None:
        state_callbacks.on_api_encryption_change(self, name, encryption)

    def _on_config_hash_change(self, name: str, config_hash: str) -> None:
        state_callbacks.on_config_hash_change(self, name, config_hash)

    def _on_importable_added(self, device: AdoptableDevice) -> None:
        importable.on_importable_added(self, device)

    def _on_importable_removed(self, name: str) -> None:
        importable.on_importable_removed(self, name)

    def get_importable_devices(self) -> list[AdoptableDevice]:
        """Snapshot of the current importable list (used for ``initial_state``)."""
        return importable.get_importable_devices(self)

    def _on_firmware_job_completed(self, event: Event[JobLifecycleData]) -> None:
        firmware_sync.on_job_completed(self, event)

    async def _refresh_after_firmware_job(
        self, configuration: str, *, recompute_hash: bool, flashed: bool
    ) -> None:
        await firmware_sync.refresh_after_job(
            self, configuration, recompute_hash=recompute_hash, flashed=flashed
        )

    async def _persist_expected_config_hash(self, configuration: str) -> None:
        await firmware_sync.persist_expected_config_hash(self, configuration)

    async def _sync_deployed_state_after_flash(self, configuration: str) -> None:
        await firmware_sync.sync_deployed_state_after_flash(self, configuration)

    def _schedule_version_reprobe(self, configuration: str) -> None:
        firmware_sync.schedule_version_reprobe(self, configuration)

    def _cancel_reprobe_timers(self) -> None:
        """Cancel any pending post-flash re-probe timers."""
        for handle in self._reprobe_timers.values():
            handle.cancel()
        self._reprobe_timers.clear()

    def _persist_build_size(self, configuration: str, result: BuildSizeRefreshResult) -> None:
        """Merge a fresh build-size triple into the metadata store."""
        self._metadata_store.update(
            configuration,
            build_size_bytes=result.size_bytes,
            build_size_dir_mtime=result.signal.dir_mtime,
            build_size_info_mtime=result.signal.info_mtime,
        )

    def _load_ignored_devices(self) -> None:
        importable.load_ignored_devices(self)

    def _save_ignored_devices(self) -> None:
        importable.save_ignored_devices(self)

    async def _archive_single(self, configuration: str) -> None:
        await archive.archive_single(self, configuration)

    async def _unarchive_single(self, configuration: str) -> None:
        await archive.unarchive_single(self, configuration)

    def _list_archived_sync(self) -> list[dict[str, Any]]:
        return archive.list_archived_sync(self)

    async def _delete_archived_single(self, configuration: str) -> None:
        await archive.delete_archived_single(self, configuration)

    async def _delete_single(self, configuration: str) -> None:
        await archive.delete_single(self, configuration)

    async def _stream_subprocess(
        self,
        cmd: list[str],
        client: Any,
        message_id: str,
        *,
        line_transform: Callable[[str], str] | None = None,
    ) -> None:
        await logs.stream_subprocess(cmd, client, message_id, line_transform=line_transform)
