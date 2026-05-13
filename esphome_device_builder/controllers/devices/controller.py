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
from typing import TYPE_CHECKING, Any, Literal

from esphome.helpers import write_file as atomic_write_file
from esphome.storage_json import StorageJSON
from esphome.zeroconf import AsyncEsphomeZeroconf

from ...helpers.api import CommandError, api_command
from ...helpers.device_yaml import (
    configuration_stem,
    generate_device_yaml,
    generate_minimal_stub_yaml,
    parse_esphome_meta,
    parse_platform_from_yaml,
)
from ...helpers.event_bus import Event
from ...helpers.mac_addresses import derive_interface_macs
from ...helpers.storage_path import resolve_storage_path
from ...helpers.yaml import (
    YamlUpsertNotSupportedError,
    generate_api_encryption_key,
    rewrite_api_encryption_key,
    rewrite_name_or_substitution,
    upsert_yaml_leaf_under_top_block,
)
from ...models import (
    AddComponentResponse,
    AdoptableDevice,
    Device,
    DeviceEventData,
    DeviceReachabilityData,
    DevicesResponse,
    DeviceState,
    DeviceStateChangedData,
    ErrorCode,
    EventType,
    JobLifecycleData,
    UpdateDeviceResponse,
    WizardResponse,
)
from .._build_size_refresher import BuildSizeRefresher
from .._device_mqtt_coordinator import DeviceMqttCoordinator
from .._device_scanner import DeviceScanner, ScanChange
from .._device_state_monitor import DeviceStateMonitor
from .._reachability_tracker import ReachabilityTracker
from ..config import (
    get_device_metadata,
    remove_device_metadata,
    set_device_labels,
    set_device_metadata,
)
from ..firmware.helpers import _find_esphome_cmd
from . import (
    add_component,
    api_key,
    archive,
    firmware_sync,
    importable,
    logs,
    reachability,
    storage_regen,
)
from ._yaml_search import (
    DEFAULT_CONTEXT_LINES,
    MAX_CONTEXT_LINES,
    search_yaml_devices,
)
from ._yaml_search_cache import YamlSearchCache
from .helpers import (
    _build_address_cache_args,
    _redact_concealed_secrets,
    _rewrite_required_yaml_leaf,
    _validate_archive_configuration,
    friendly_name_slugify,
)
from .metadata import DeviceMetadataMixin

if TYPE_CHECKING:
    from ...device_builder import DeviceBuilder
    from ...models import BoardCatalogEntry

_LOGGER = logging.getLogger(__name__)

# Provenance tag for ``_yaml_content_for_create``'s return tuple.
# ``"user"`` → caller-supplied ``file_content`` (validation
# failure surfaces as ``INVALID_ARGS``).
# ``"template"`` → :func:`generate_device_yaml` against a known
# catalog entry (validation failure → ``INTERNAL_ERROR``).
# ``"stub"`` → :func:`generate_minimal_stub_yaml` (no inputs;
# validation failure → ``INTERNAL_ERROR``; caller skips the YAML-
# driven board-id derivation since the stub's hard-coded
# ``board: esp32dev`` would otherwise pin metadata to whatever
# catalog entry happens to share that PIO board).
_CreateYamlSource = Literal["user", "template", "stub"]

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

# Per-file match cap for ``yaml/search``. Each device contributes
# at most this many lines so a chatty match (a query of ``:``
# against a deeply-nested config) doesn't drown hits in other
# devices. The dropdown caps its overall hit count at the
# caller-supplied ``max_results`` on top of this.
_YAML_SEARCH_PER_FILE_MATCH_CAP = 5


class DevicesController(  # noqa: PLR0904 (grandfathered; new public methods need a refactor first)
    DeviceMetadataMixin,
):
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
        # up background tasks. Constructed after the scanner so
        # ``on_refreshed=self._scanner.reload`` is bindable.
        self._build_size = BuildSizeRefresher(
            config_dir=self._db.settings.config_dir,
            get_filenames=lambda: (d.configuration for d in self._get_devices()),
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
            on_version_change=self._on_version_change,
            on_config_hash_change=self._on_config_hash_change,
            on_api_encryption_change=self._on_api_encryption_change,
            on_mac_address_change=self._on_mac_address_change,
            on_importable_added=self._on_importable_added,
            on_importable_removed=self._on_importable_removed,
            is_ignored=self.ignored_devices.__contains__,
            presence=self._db.subscriber_presence,
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
        # Build-size worker — runs its own initial fleet sweep
        # on first iteration to pick up CLI-compile drift, then
        # drains per-device requests as they arrive from the
        # job-completion hook.
        self._build_size.start()

    async def stop(self) -> None:
        """Stop background monitors so the process exits cleanly."""
        if self._unsub_job_completed is not None:
            self._unsub_job_completed()
            self._unsub_job_completed = None
        await self._build_size.stop()
        await self._mqtt_coordinator.stop()
        await self._state_monitor.stop()

    async def poll(self) -> None:
        """Poll for file changes."""
        await self._scanner.scan()
        await self._mqtt_coordinator.reconcile()

    def get_devices(self) -> list[Device]:
        """Snapshot of the currently-loaded devices."""
        return self._scanner.devices

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
        importable = [d for d in self.import_result.values() if d.name not in configured_names]
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
        """
        Substring-search every configured device's raw YAML file.

        Returns a list of per-device hits, each entry shaped as::

            {
              "configuration": "<filename>",
              "device_name":   "<esphome.name>",
              "friendly_name": "<esphome.friendly_name or name>",
              "matches": [
                {"line_number": <1-based int>, "line_text": "<raw line>"}
              ]
            }

        Per-file matches are capped at
        ``_YAML_SEARCH_PER_FILE_MATCH_CAP`` so a chatty match (e.g.
        a query of ``:`` against a deeply-nested config) doesn't
        crowd out hits in other devices, and the total hit count is
        capped at ``max_results`` so the dropdown on the frontend
        stays usable. Empty / whitespace-only queries return ``[]``
        immediately — the frontend debounces typing but a stray
        empty call shouldn't iterate every YAML file.

        Reads the on-disk file (not the package-resolved tree) for
        cheap line-numbered grep. Searching expanded packages would
        need separate "matched in package X line Y" rendering on the
        frontend; queued as a follow-up.

        Iterates the scanner's existing snapshot rather than firing
        a fresh ``await self._scanner.scan()`` like ``devices/list``
        does — this command runs once per debounced keystroke from
        the frontend's command palette, and a per-keystroke disk
        scan would dominate the round-trip cost. The scanner refreshes
        on its own cadence (file-watcher events + periodic re-scan)
        so YAMLs added or removed between scans become visible on
        the next scan, not the next search. The scanner-level skip
        of YAMLs that fail to materialise into a ``Device`` (broken
        configs that ``DeviceScanner._load_devices()`` logs and
        drops) carries through here too: this command searches the
        same set of devices the dashboard list shows, not the raw
        ``*.yaml`` filesystem.

        Cache: see ``_yaml_search_cache.YamlSearchCache``. The
        frontend debounces keystrokes but still fires one search
        per pause — on a fleet of 100 devices that's 100 reads + 100
        splitlines per keystroke without a cache. With it, every
        keystroke after the first becomes a stat-and-grep against
        an already-split list (only files whose mtime changed get
        re-read).
        """
        needle_raw = query.strip()
        if not needle_raw:
            return []
        needle = needle_raw if case_sensitive else needle_raw.lower()

        # Server-clamp the context window. The frontend can dial
        # this between ``0`` (no context, just the matched line)
        # and ``MAX_CONTEXT_LINES`` to render denser or sparser
        # snippets without a backend redeploy. ``None`` means the
        # caller didn't ask — use ``DEFAULT_CONTEXT_LINES``.
        # Out-of-range values clamp to the nearest endpoint
        # (negative → 0, > MAX → MAX) rather than falling back
        # to the default: a caller passing ``10_000`` clearly
        # wants "as much context as possible", and giving them
        # ``MAX_CONTEXT_LINES`` is closer to that intent than
        # silently substituting ``DEFAULT_CONTEXT_LINES``.
        if context_lines is None:
            effective_context_lines = DEFAULT_CONTEXT_LINES
        else:
            effective_context_lines = max(0, min(context_lines, MAX_CONTEXT_LINES))

        # Global search lock: serialise the I/O-bound walk so two
        # concurrent searches don't double up on stat / read calls
        # against the same fleet. The frontend's per-keystroke
        # debounce + concurrency-of-1 gate keeps the queue shallow
        # in normal use; this lock backstops the case where a slow
        # request from a stuck client overlaps with a fresh one.
        async with self._yaml_search_lock:
            results, live_configurations = await search_yaml_devices(
                devices=self._scanner.devices,
                cache=self._yaml_search_cache,
                rel_path=lambda c: Path(self._db.settings.rel_path(c)),
                needle=needle,
                case_sensitive=case_sensitive,
                max_results=max_results,
                per_file_cap=_YAML_SEARCH_PER_FILE_MATCH_CAP,
                context_lines=effective_context_lines,
            )
            self._yaml_search_cache.prune(live_configurations)
            return results

    # ------------------------------------------------------------------
    # API commands — CRUD
    # ------------------------------------------------------------------

    @api_command("devices/create")
    async def create_device(  # noqa: PLR0915
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
        3. Neither given → emit a minimal valid esp32 stub via
           :func:`generate_minimal_stub_yaml`. The wizard's
           "Empty Configuration — for manually writing or pasting"
           button hits this path; the user wants a starter they
           can fully rewrite, but the starter must validate so
           downstream operations don't refuse it. esp32 is the
           default platform (most common); a leading comment in
           the stub tells the user to swap the platform block if
           their hardware differs.

        The two *generated* flows (template, stub) run the result
        through ``EditorController``'s schema check before the
        file lands on disk — those are *our* outputs, so an
        invalid one is our regression to fix and surfaces as
        ``INTERNAL_ERROR``.

        The user-upload flow deliberately skips validation. The
        whole point of "upload an existing YAML" is to get a
        config the user already has into the dashboard so they
        can edit it; many real-world cases are configs from older
        ESPHome versions whose components have since changed
        schema, and refusing to write them would lock the user
        out of the editor — the only place they can fix the YAML
        in the first place. The next compile / install will
        surface the actual schema errors with line numbers, which
        is what the user wants when they're repairing an old
        config.

        After writing, we always try to derive a board_id by parsing
        the resulting YAML's platform/board/variant fields and matching
        against the catalog. The derived (or supplied) board_id is
        stored in metadata for later reference.
        """
        name = name.strip()
        if not name:
            raise CommandError(ErrorCode.INVALID_ARGS, "name is required")

        filename = f"{name}.yaml"
        config_path = self._db.settings.rel_path(filename)

        # Fast collision check before the (~hundreds of ms) validator
        # round-trip so a duplicate-name attempt fails on the right
        # diagnostic instead of surfacing a "config doesn't validate"
        # for a YAML we weren't about to write anyway. The ``open(...,
        # "x")`` further down is the actual race-safe write — the
        # check here is a UX optimisation, not a TOCTOU guard.
        loop_for_check = asyncio.get_running_loop()
        if await loop_for_check.run_in_executor(None, config_path.exists):
            msg = f"Configuration {filename} already exists"
            raise CommandError(ErrorCode.INVALID_ARGS, msg)

        # Surface user-correctable failures (unknown board, name
        # collision) as typed ``INVALID_ARGS`` so the wizard can show
        # a specific message instead of the WS layer's generic
        # "Command failed" fallback. The collision check happens at
        # write time below via exclusive-create — see there for why.
        board = None
        if board_id:
            if self._db.boards:
                board = await self._db.boards.get_board(board_id=board_id)
            if board is None:
                msg = f"Unknown board: {board_id}"
                raise CommandError(ErrorCode.INVALID_ARGS, msg)

        friendly = friendly_name_slugify(name)
        yaml_content, source = self._yaml_content_for_create(
            name, friendly, board, file_content, ssid, psk
        )

        # Validate generated YAML before write so a regression in
        # ``generate_device_yaml`` / ``generate_minimal_stub_yaml``
        # surfaces as ``INTERNAL_ERROR`` (our bug to report) rather
        # than landing an unflashable YAML on disk. User uploads
        # are deliberately *not* validated here — the upload flow
        # exists so users can bring an existing config (often from
        # an older ESPHome version with since-changed component
        # schemas) into the builder and repair it in the editor.
        # Refusing the write would strand them; the next compile
        # / install surfaces the real schema errors with line
        # numbers, which is what they need to fix it.
        if source != "user":
            await self._validate_rewritten_yaml_or_raise(
                filename,
                yaml_content,
                action="create",
                on_failure=ErrorCode.INTERNAL_ERROR,
            )

        # Derive board_id from YAML when not explicitly provided.
        # Mirrors the scanner's resolution chain: pio_board match first,
        # then platform+variant fallback for generic ``esp32:``-style
        # configs without a specific PlatformIO board id.
        #
        # Skip derivation for the stub branch. ``generate_minimal_stub_yaml``
        # hard-codes ``esp32: board: esp32dev`` because the user
        # hasn't picked hardware yet, but many catalog entries share
        # that PIO board — running the lookup would pin the new
        # device to whatever catalog entry the index happens to
        # surface first, and the wrong entry would stay bound even
        # after the user rewrites the platform block. ``parsed_platform``
        # is still set so :func:`StorageJSON` gets a sensible
        # ``target_platform`` for the initial sidecar.
        parsed_platform = ""
        if not board_id and self._db.boards:
            parsed_platform, pio_board, variant = parse_platform_from_yaml(yaml_content)
            if source != "stub":
                matched = None
                if pio_board:
                    matched = self._db.boards.find_by_pio_board(pio_board, variant)
                if matched is None and parsed_platform:
                    matched = self._db.boards.find_by_platform_variant(parsed_platform, variant)
                if matched:
                    board = matched
                    board_id = matched.id

        loop = asyncio.get_running_loop()

        def _write_exclusive() -> None:
            # Exclusive-create so a concurrent ``devices/create`` (or
            # any other writer) can't slip between a preflight check
            # and the write and silently clobber an in-flight config.
            with open(config_path, "x", encoding="utf-8") as f:
                f.write(yaml_content)

        try:
            await loop.run_in_executor(None, _write_exclusive)
        except FileExistsError as exc:
            msg = f"Configuration {filename} already exists"
            raise CommandError(ErrorCode.INVALID_ARGS, msg) from exc

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
            storage_path = resolve_storage_path(filename)
            storage_path.parent.mkdir(parents=True, exist_ok=True)
            storage.save(storage_path)

            # Clear any residual metadata entry under this filename
            # before we write the new one. Archive preserves
            # identity fields (``board_id`` / ``friendly_name`` /
            # ``comment``) so an unarchive of the same YAML restores
            # state, but a *new* device created at the same filename
            # must start fresh — otherwise an archived device's
            # ``board_id`` would silently mis-bind the new device's
            # YAML to the wrong catalog entry, and the persisted
            # ``friendly_name`` would override the new YAML's. The
            # stub create path (no ``board_id`` provided, no derive
            # match) wouldn't otherwise overwrite the entry, so the
            # explicit wipe runs unconditionally.
            remove_device_metadata(self._db.settings.config_dir, filename)
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
        await self._persist_device_metadata_async(
            filename,
            board_id=board_id,
            friendly_name=friendly_name,
            comment=comment,
        )

        # ``get_device_metadata`` reads ``.device-builder.json`` via
        # ``Path.read_bytes()``; route it through the executor so the
        # sync I/O doesn't stall the event loop (and doesn't trip
        # blockbuster on Linux CI).
        config_dir = self._db.settings.config_dir
        meta = await asyncio.to_thread(get_device_metadata, config_dir, filename)
        return UpdateDeviceResponse(
            name=name,
            friendly_name=meta.get("friendly_name", name),
            comment=meta.get("comment"),
            board_id=meta.get("board_id"),
        )

    @api_command("devices/set_labels")
    async def set_labels(
        self,
        *,
        configuration: str,
        label_ids: list[str],
        **kwargs: Any,
    ) -> Device:
        """
        Replace this device's label assignments.

        ``label_ids`` is the new full list of assigned label IDs (no
        diff semantics — ``[]`` clears every assignment). Unknown
        IDs are rejected with ``INVALID_ARGS``; the catalog check
        runs inside the same metadata transaction as the write so a
        concurrent ``labels/delete`` cascade can't leave a dangling
        reference. After persistence the device is force-reloaded
        so the live ``Device`` model reflects the new labels — the
        scanner's mtime cache would otherwise skip the file.
        """
        # ``rel_path`` raises ``CommandError(INVALID_ARGS)`` on path
        # traversal; reuses the existing single chokepoint.
        self._db.settings.rel_path(configuration)
        if not isinstance(label_ids, list):
            raise CommandError(
                ErrorCode.INVALID_ARGS, "label_ids must be a list of label id strings"
            )

        # Verify the device exists *before* writing the sidecar — a
        # ``configuration`` that passes ``rel_path`` but isn't tracked
        # by the scanner (typo, deleted YAML) would otherwise leave an
        # orphaned ``.device-builder.json`` entry pinning labels to a
        # non-existent device. The scanner's name index is the
        # authoritative "what's actually on disk" view.
        device = next(
            (d for d in self._scanner.devices if d.configuration == configuration),
            None,
        )
        if device is None:
            raise CommandError(ErrorCode.NOT_FOUND, f"Device {configuration!r} not found")

        config_dir = self._db.settings.config_dir

        def _persist() -> None:
            try:
                set_device_labels(config_dir, configuration, label_ids)
            except ValueError as err:
                raise CommandError(ErrorCode.INVALID_ARGS, str(err)) from err

        await asyncio.to_thread(_persist)
        await self._scanner.reload(configuration)

        # Re-fetch from the scanner — ``reload`` replaces the Device
        # in the index, so the reference held above is stale.
        refreshed = next(
            (d for d in self._scanner.devices if d.configuration == configuration),
            None,
        )
        if refreshed is None:
            raise CommandError(ErrorCode.NOT_FOUND, f"Device {configuration!r} not found")
        return refreshed

    @api_command("devices/rename")
    async def rename_device(
        self,
        *,
        configuration: str,
        new_name: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        Rename a device configuration.

        Thin pass-through to ``esphome rename``: the CLI owns the
        whole atomic flow — YAML edit (substitution-aware), config
        revalidation, compile + OTA install, and rollback (unlinks
        the freshly-written YAML on validation or install failure
        so the device stays reachable under its old hostname).
        Routed through the firmware queue so the streaming output
        shows up alongside other firmware tasks instead of running
        silently in the background.

        We deliberately don't pre-validate or fall back to a
        file-level rename when the config doesn't validate. The CLI
        already runs its own ``esphome config`` pass and surfaces
        the errors verbatim; a file-level fallback would silently
        rename the YAML on disk while leaving the running firmware
        broadcasting the old hostname forever (dashboard label and
        device state diverge with no error to the user). Better to
        error than to make a potentially broken config worse —
        matches the legacy dashboard's thin-pass-through behaviour.

        Returns the new filename plus the queued firmware job.
        """
        new_filename = f"{new_name}.yaml"

        # Reject same-name renames up-front: a no-op at the YAML
        # level but still queues a real ``esphome rename`` job that
        # re-compiles and OTA-flashes the device. Frontend should
        # call ``firmware/install`` for "flash without renaming".
        # Compare on the *stem*, not the filename, so cloning
        # ``kitchen.yml`` to ``new_name=kitchen`` is rejected even
        # though the literal filenames differ — the device's mDNS
        # hostname comes from the stem and stays the same either
        # way, so the rename would still be a no-op rewrite + flash.
        source_stem = configuration_stem(configuration)
        if new_name == source_stem:
            raise CommandError(
                ErrorCode.INVALID_ARGS,
                "new_name must differ from the current device name",
            )
        # Reject up-front if the target filename is already in use.
        # ``esphome rename`` itself doesn't check collisions — it
        # blindly ``write_text``s the new YAML and OTA-installs it,
        # so without this check we'd silently overwrite an unrelated
        # device's config and flash that firmware to the wrong
        # device. ``rel_path`` resolves the filename and
        # ``.exists()`` is an ``os.stat`` — both blocking. Push the
        # pair to the executor so the dashboard's request-path
        # stays event-loop-friendly on slow / network-mounted dirs.
        loop = asyncio.get_running_loop()
        new_path = self._db.settings.rel_path(new_filename)
        if await loop.run_in_executor(None, new_path.exists):
            msg = f"A device named {new_filename} already exists"
            raise CommandError(ErrorCode.INVALID_ARGS, msg)

        if self._db.firmware is None:
            msg = "Firmware controller is unavailable"
            raise CommandError(ErrorCode.INTERNAL_ERROR, msg)
        job = await self._db.firmware.rename(configuration=configuration, new_name=new_name)
        return {"configuration": new_filename, "job": job.to_dict()}

    @api_command("devices/clone")
    async def clone_device(
        self,
        *,
        configuration: str,
        new_name: str,
        new_friendly_name: str | None = None,
        **kwargs: Any,
    ) -> dict[str, str]:
        """
        Duplicate an existing device YAML under a fresh hostname.

        Designed for "I bought 10 of the same bulb" workflows: the
        clone copies the source YAML's components and wiring intact
        but takes a fresh ``esphome.name`` (and therefore a fresh
        mDNS hostname / API endpoint), a fresh ``friendly_name``,
        and a freshly-generated ``api.encryption.key`` so two
        siblings forked from the same source don't share encryption
        material — compromise of one device must not compromise
        the others.

        ``new_friendly_name`` is optional: when omitted, defaults
        to ``friendly_name_slugify(new_name)`` to mirror how
        ``devices/create`` derives the wizard friendly_name. Pass
        a blank string to leave the source's ``friendly_name:``
        line untouched (rare — most callers want a per-clone label).

        Indirections (``key: !secret api_key`` /
        ``key: ${api_key}``) are deliberately preserved on the
        clone — the indirection target is shared with the source
        on disk, so rewriting the indirection name to a fresh
        literal would silently desync the rendered config from
        whatever ``secrets.yaml`` / substitutions block actually
        drives the encryption.

        Returns ``{"configuration": "<new_filename>"}``. Errors
        propagate as ``INVALID_ARGS`` for collisions / empty
        names; the source file failing to load surfaces as
        ``INTERNAL_ERROR``.
        """
        new_name = new_name.strip()
        if not new_name:
            raise CommandError(ErrorCode.INVALID_ARGS, "new_name is required")
        new_filename = f"{new_name}.yaml"
        # Compare on the *stem*, not the filename, so cloning
        # ``kitchen.yml`` to ``new_name=kitchen`` is rejected even
        # though the filenames differ — both files would still carry
        # the same ``esphome.name`` and collide on mDNS.
        source_stem = configuration_stem(configuration)
        if new_name == source_stem:
            raise CommandError(
                ErrorCode.INVALID_ARGS,
                "new_name must differ from the source device name",
            )

        loop = asyncio.get_running_loop()
        source_path = self._db.settings.rel_path(configuration)
        new_path = self._db.settings.rel_path(new_filename)
        config_dir = self._db.settings.config_dir
        # Default the friendly_name to a slug-derived fallback so
        # the dashboard list doesn't show two entries with the same
        # label after a clone. Explicit blank string opts out.
        if new_friendly_name is None:
            new_friendly_name = friendly_name_slugify(new_name)
        # Fresh encryption material per clone — generated up here
        # rather than inside the executor so the off-loop work is
        # purely I/O.
        new_key = generate_api_encryption_key()

        # All blocking I/O bundled into one executor hop. Returns the
        # (raw_source_content, source_metadata, target_existed_at_start)
        # triple — the caller maps each into either a typed
        # ``CommandError`` or the rewrite + write step that follows.
        # ``ext_storage_path`` / metadata helpers / file I/O are all
        # ``Path``-walking syscalls under the hood; no point in five
        # round-trips through ``run_in_executor`` when one will do.
        def _gather() -> tuple[str | None, dict | None, bool]:
            if new_path.exists():
                return None, None, True
            if not source_path.exists():
                return None, None, False
            content = source_path.read_text(encoding="utf-8")
            meta = get_device_metadata(config_dir, configuration)
            return content, meta, False

        source_content, source_meta, target_existed = await loop.run_in_executor(None, _gather)
        if target_existed:
            msg = f"A device named {new_filename} already exists"
            raise CommandError(ErrorCode.INVALID_ARGS, msg)
        if source_content is None:
            msg = f"Source device {configuration} not found"
            raise CommandError(ErrorCode.INVALID_ARGS, msg)

        # Validate the *source* before doing any rewrite work. The
        # leaf-line rewrites that follow (name / friendly_name / api
        # key) are structure-preserving, so a valid source always
        # produces a valid clone — and an invalid source always
        # produces an invalid clone. Bail early on a broken source
        # with a message that points at the source's actual schema
        # errors, so the user fixes the source first and retries.
        # Surfacing this here also means we don't burn the rewrite
        # work just to re-discover the source was unflashable.
        await self._validate_rewritten_yaml_or_raise(configuration, source_content, action="clone")

        # Land the new identity on whichever line the source actually
        # uses to drive the value. Two patterns appear in real configs:
        #
        # 1. **Direct literal** — ``esphome.name: kitchen``. Rewrite
        #    the leaf line.
        # 2. **Substitution reference** — ``esphome.name: ${devicename}``
        #    with ``substitutions.devicename: kitchen``. This is the
        #    standard ESPHome wizard / dashboard_import shape. Here
        #    the *leaf* line carries an indirection name; the actual
        #    value lives in the substitutions block. Rewriting the
        #    leaf would land a literal name and silently orphan the
        #    substitution, breaking any other consumer of the same
        #    variable (a sensor named ``${devicename}_temp``, etc.).
        #    Rewrite the substitution definition instead so every
        #    reference re-targets atomically.
        #
        # Mixed values (``${prefix}-suffix``) aren't pure references
        # and fall through to the leaf rewrite — we have no way to
        # split the prefix without changing the suffix's meaning.
        # ``_rewrite_required_yaml_leaf`` folds in the "leaf must
        # exist in this file" precondition + the rewrite. Reject
        # is the right behaviour for clone — a hostname rewrite
        # that silently no-ops on a package-driven source would
        # produce a duplicate device under the source's hostname
        # and collide on mDNS. (The friendly-name editor takes the
        # opposite call via ``upsert_yaml_leaf_under_top_block``
        # — for a display label, ESPHome's package merge means
        # *inserting* a local leaf is what the user wants.)
        new_content = _rewrite_required_yaml_leaf(source_content, ("esphome", "name"), new_name)
        # ``friendly_name`` is optional on the clone path — when
        # the source doesn't have an inline leaf the clone just
        # ships without one (vs. ``name`` which we hard-require).
        # Skip the helper here; the underlying ``rewrite_name_or_substitution``
        # is already a no-op when the leaf is missing.
        if new_friendly_name:
            new_content = rewrite_name_or_substitution(
                new_content, ("esphome", "friendly_name"), new_friendly_name
            )
        # ``rewrite_api_encryption_key`` is a no-op when the source
        # uses ``!secret`` / ``${...}`` for the key — those
        # indirections stay shared with the source on purpose.
        new_content = rewrite_api_encryption_key(new_content, new_key)

        # Carry forward only the source's ``board_id`` — that's the
        # one piece of dashboard state the scanner can't recover from
        # the YAML (it's a catalog-key indirection the user picked at
        # wizard time). Friendly name lives in the YAML we just
        # wrote, so ``load_device_from_storage`` reads it back on the
        # next scan; copying to metadata too would just duplicate
        # truth. ``ip`` is deliberately not carried — the clone
        # hasn't booted yet, and inheriting the source's address
        # would mis-route ``devices/logs`` until the first mDNS
        # announce corrects it. StorageJSON is skipped entirely: it's
        # a build artefact, the next compile writes a real one, and
        # ``load_device_from_storage`` handles a missing sidecar by
        # reading from YAML alone.
        carry_board_id = source_meta.get("board_id") if source_meta else None

        def _commit() -> None:
            with open(new_path, "x", encoding="utf-8") as f:
                f.write(new_content)
            if carry_board_id:
                set_device_metadata(config_dir, new_filename, board_id=carry_board_id)

        try:
            await loop.run_in_executor(None, _commit)
        except FileExistsError as exc:
            # Race: another caller created the file between our
            # gather pass and the ``open(... "x")``. Surface as the
            # same INVALID_ARGS the preflight produces so the
            # frontend renders a single message.
            msg = f"A device named {new_filename} already exists"
            raise CommandError(ErrorCode.INVALID_ARGS, msg) from exc
        # Rescan so the scanner indexes the new YAML and fires the
        # ADDED event the WS subscribers expect. ``probe_device``
        # runs from the scan-change handler — no double-probe here.
        await self._scanner.scan()
        return {"configuration": new_filename}

    @api_command("devices/edit_friendly_name")
    async def edit_friendly_name(
        self,
        *,
        configuration: str,
        new_friendly_name: str,
        **kwargs: Any,
    ) -> dict[str, str | bool]:
        """
        Rewrite ``esphome.friendly_name:`` in the device YAML.

        Targeted at the "I just want to call my bulb something
        different" workflow — beginners shouldn't have to open the
        YAML editor and find the right line to change a label.

        The YAML is the source of truth: sidecar-only updates would
        let the dashboard label drift from what the running firmware
        broadcasts (every reboot would announce the YAML's value via
        mDNS, the next compile bakes the YAML in, dashboard and
        device disagree). Reuses the
        :func:`rewrite_name_or_substitution` helper from the clone
        path so the substitution-redirect / safe-quoting / list-
        item-aware machinery stays in one place.

        Doesn't touch firmware — the frontend already owns the
        install-flow UX (opens the command-dialog, follows the
        streaming job, surfaces toasts) so a separate
        ``firmware/install`` call after this one composes
        naturally with the existing rename / install paths and
        keeps this command's responsibility narrow.

        Returns ``{"configuration": …, "rewritten": bool}``.
        ``rewritten`` is False when the leaf already matched the
        requested value (no-op rewrite); the caller can use that
        to skip a redundant follow-up install.

        Insertion behaviour for absent leaves:
        - Existing ``esphome.friendly_name`` line → rewritten in
          place (substitution-aware via
          :func:`rewrite_name_or_substitution`).
        - Existing ``esphome:`` block but no ``friendly_name:``
          child → ``friendly_name:`` is inserted into the block.
        - No ``esphome:`` block at all (package / ``!include``-
          driven config) → a new ``esphome:`` block is prepended
          with just ``friendly_name:``. ESPHome's package merge
          gives our local leaf precedence over the package's
          value, so the user's intended override actually lands.
          ``esphome.name`` is intentionally not synthesised — a
          literal-text check can't see a name supplied by
          ``packages:`` / ``!include`` / substitutions, and a
          synthesised slug here would silently override the
          package-supplied hostname (breaking API discovery,
          OTA, and mDNS). Configs that genuinely lack ``name:``
          from any source are already invalid and ESPHome's
          schema check will report it on the next compile.

        User-correctable failures raise typed
        ``CommandError(INVALID_ARGS, …)``:
        - blank ``new_friendly_name``
        - source not found
        - source's ``esphome:`` is in flow-style
          (``esphome: { … }``) or a tagged value
          (``esphome: !include …``); the line-based upsert can't
          safely edit either shape, so the user has to convert
          to block style first.
        - the rewritten YAML doesn't pass ESPHome's config
          validation. The friendly_name only reaches the device
          via the next install, so writing a YAML that won't
          compile would leave the dashboard stuck displaying the
          old label forever (no fresh mDNS broadcast to update
          the running firmware's announced name). Refusing the
          write here is the only way to keep dashboard label and
          device hostname in sync; the dialog surfaces the
          validation errors so the user can fix them in the
          editor and retry.
        """
        new_friendly_name = new_friendly_name.strip()
        if not new_friendly_name:
            raise CommandError(ErrorCode.INVALID_ARGS, "new_friendly_name is required")

        loop = asyncio.get_running_loop()
        config_path = self._db.settings.rel_path(configuration)

        def _read() -> str | None:
            # Single ``read_text`` call — no preceding ``exists()``
            # check, since a file deleted between the two would
            # leak ``FileNotFoundError`` past us as an unhandled
            # exception (surfaces as ``INTERNAL_ERROR`` instead of
            # the typed ``INVALID_ARGS`` we want for "device gone").
            # Catching here folds the race + the genuinely-missing
            # case into the same branch.
            try:
                return config_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return None

        content = await loop.run_in_executor(None, _read)
        if content is None:
            raise CommandError(
                ErrorCode.INVALID_ARGS,
                f"Device {configuration} not found",
            )

        # ``upsert_yaml_leaf_under_top_block`` handles three shapes:
        # rewrite an existing leaf (substitution-aware), insert
        # ``friendly_name:`` into an existing ``esphome:`` block,
        # or prepend a new ``esphome:`` block when the YAML doesn't
        # have one (package / ``!include``-driven configs). For a
        # display label the override-from-package case is exactly
        # what the user wants — they're saying "call THIS device
        # something different" — and ESPHome's package merge gives
        # our local leaf precedence over the included one.
        #
        # We deliberately don't try to synthesise ``esphome.name``
        # for configs where the literal YAML doesn't have one. A
        # text-level check can't see ``name:`` supplied by
        # ``packages:`` / ``!include`` / substitutions, and a
        # synthesised slug landing here would silently override
        # the package-supplied hostname — breaking API discovery,
        # OTA, and mDNS without warning. If a config genuinely
        # has no ``name:`` from any source, ESPHome's schema will
        # surface "required key not provided" on the next compile,
        # which the user can address explicitly.
        try:
            new_content = upsert_yaml_leaf_under_top_block(
                content, "esphome", "friendly_name", new_friendly_name
            )
        except YamlUpsertNotSupportedError as exc:
            # Flow-style ``esphome: { ... }`` or a tagged value
            # (``esphome: !include …``). The line-based walker
            # can't safely insert into either shape — surface as
            # an actionable error so the dialog tells the user to
            # switch to block style rather than landing a
            # duplicate ``esphome:`` key.
            raise CommandError(ErrorCode.INVALID_ARGS, str(exc)) from exc

        # Round-trip check: parse the rewritten YAML through the
        # same reader the scanner uses (``parse_esphome_meta``) and
        # verify it sees the new ``friendly_name``. Cheap defence
        # against the line-based upsert producing a YAML shape that
        # serializes fine but the reader misinterprets — a real bug
        # we shipped once where wizard-emitted column-0 ``# Board:``
        # / ``# Definition:`` comments ended up between an inserted
        # ``name:`` and ``friendly_name:``, the reader hit ``# Board:``
        # at column 0, treated it as a fresh top-level key, dropped
        # the ``esphome:`` context, and silently lost
        # ``friendly_name`` on every subsequent load. The user saw
        # "renamed but the dashboard still shows the old name." Run
        # the verification before writing so a future rewriter bug
        # surfaces as a typed error instead of silently corrupting
        # the user's config.
        _, parsed_friendly, _, _ = parse_esphome_meta(new_content)
        if parsed_friendly != new_friendly_name:
            raise CommandError(
                ErrorCode.INTERNAL_ERROR,
                "Edited YAML doesn't round-trip through the reader — "
                "the line-based upsert produced a shape the parser "
                "misinterprets. This is a dashboard bug; please file "
                "an issue with a redacted snippet of just the "
                "esphome: / substitutions: blocks (strip Wi-Fi "
                "credentials, API keys, and static IPs) so we can "
                "extend the rewriter's coverage.",
            )
        if new_content == content:
            # Idempotent — user submitted the same value (or the
            # leaf was already that value). Skip the write and
            # signal to the caller that no install is needed
            # either; the dialog can close without queuing a
            # redundant OTA job. Skip the validation pass too —
            # the file isn't changing, so revalidating just to
            # mirror its existing state would burn ~hundreds of
            # ms on the editor subprocess for nothing.
            return {"configuration": configuration, "rewritten": False}

        # Same-shape rewrites that don't actually change anything
        # in the YAML aren't worth the validator round-trip — see
        # ``_validate_rewritten_yaml_or_raise``. Run validation
        # here, before the write, so a YAML that won't compile is
        # rejected with the editor's actual errors instead of
        # silently landing on disk for an install that will never
        # take effect.
        await self._validate_rewritten_yaml_or_raise(
            configuration, new_content, action="update friendly name"
        )

        await self._persist_yaml_mutation(configuration, new_content)
        return {"configuration": configuration, "rewritten": True}

    def _yaml_content_for_create(
        self,
        name: str,
        friendly: str,
        board: BoardCatalogEntry | None,
        file_content: str | None,
        ssid: str,
        psk: str,
    ) -> tuple[str, _CreateYamlSource]:
        """
        Pick the YAML body for ``devices/create`` based on the inputs.

        Returns ``(yaml_content, source)``; see :data:`_CreateYamlSource`
        for the meaning of each tag and which post-processing the
        caller applies per branch.
        """
        if file_content:
            return file_content, "user"
        if board:
            return generate_device_yaml(name, friendly, board, ssid, psk), "template"
        return generate_minimal_stub_yaml(name, friendly), "stub"

    async def _validate_rewritten_yaml_or_raise(
        self,
        configuration: str,
        content: str,
        *,
        action: str,
        on_failure: ErrorCode = ErrorCode.INVALID_ARGS,
        on_error_cleanup: Callable[[], None] | None = None,
    ) -> None:
        """
        Schema-validate *content* via the editor; raise if invalid.

        Used by commands that produce a YAML and depend on a follow-
        up install to apply the change (``create_device``,
        ``edit_friendly_name``, future rename / save handlers). A
        YAML that won't validate means the install fails and the
        device-on-network keeps its old state — the dashboard ends
        up showing a half-finished change that will never reach the
        device. Refusing the write here keeps on-disk state and
        live state in lockstep.

        Reuses :class:`EditorController`'s warm validator subprocess
        (one per configuration), so the cost is single-digit hundreds
        of ms when the session is warm. ``self._db.editor`` is None
        during dashboard boot before
        :meth:`EditorController.start` finishes; that window is too
        narrow for a user to hit in practice, but the guard means
        the controller still behaves coherently if the editor is
        unavailable (e.g. the ``esphome`` CLI not on PATH) — better
        to skip validation than reject every write.

        *action* is interpolated into the error message ("Can't
        <action> — config doesn't validate: …"); pass the user-
        facing verb the dialog will show ("rename", "save",
        "create"). *on_failure* picks the ``ErrorCode`` raised:
        default ``INVALID_ARGS`` when the user can fix the input
        themselves, ``INTERNAL_ERROR`` when the broken YAML came
        from one of *our* generators (``generate_device_yaml``,
        clone's leaf rewrite, ...) and the user can't do anything
        but report it. The error list is capped at three entries so
        a long error pile collapses to "first three + (+N more)"
        rather than overflowing the toast.

        *on_error_cleanup* is a sync callback that runs in a
        ``finally`` if validation didn't reach a clean success
        (rejected, subprocess-wedged, anything). For commands that
        write the YAML *before* calling this helper (currently
        ``import_device``, where upstream ``import_config`` writes
        unconditionally), the callback unlinks the half-imported
        file so a retry doesn't trip ``FileExistsError``. Unset
        for commands that haven't written yet (``create_device``,
        ``clone_device``, ``edit_friendly_name``) — there's
        nothing to roll back. Runs through ``run_in_executor`` so
        ``blockbuster`` doesn't flag the sync syscall in tests.
        """
        editor = self._db.editor
        if editor is None:
            return
        succeeded = False
        try:
            result = await editor.validate_yaml(configuration=configuration, content=content)
            errors = [
                *(err.get("message", "") for err in result.get("yaml_errors", [])),
                *(err.get("message", "") for err in result.get("validation_errors", [])),
            ]
            errors = [msg for msg in errors if msg]
            if not errors:
                succeeded = True
                return
            shown = errors[:3]
            suffix = f" (+{len(errors) - len(shown)} more)" if len(errors) > len(shown) else ""
            message_tail = (
                ". Please report this with a redacted snippet of just the "
                "esphome: / substitutions: blocks (strip Wi-Fi credentials, "
                "API keys, and static IPs) so the dashboard generator can "
                "be fixed."
                if on_failure is ErrorCode.INTERNAL_ERROR
                else ". Fix the errors in the editor and try again."
            )
            raise CommandError(
                on_failure,
                f"Can't {action} — config doesn't validate: "
                + "; ".join(shown)
                + suffix
                + message_tail,
            )
        finally:
            if not succeeded and on_error_cleanup is not None:
                # Swallow + log cleanup failures so a permission /
                # FS error during rollback doesn't replace the
                # original validation diagnostic (or the validator
                # subprocess error) the caller is about to see. The
                # leftover YAML is the lesser foot-gun here — the
                # user can ``devices/delete`` it once they understand
                # what failed.
                try:
                    await asyncio.get_running_loop().run_in_executor(None, on_error_cleanup)
                except Exception:
                    _LOGGER.exception("on_error_cleanup raised; original error preserved")

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
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._list_archived_sync)

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
        """Read device config YAML."""
        return await self._read_yaml_async(self._db.settings.rel_path(configuration))

    @api_command("devices/update_config")
    async def update_config(self, *, configuration: str, content: str, **kwargs: Any) -> None:
        """Write device config YAML."""
        await self._persist_yaml_mutation(configuration, content)

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

    @api_command("devices/add_component")
    async def add_component(
        self,
        *,
        configuration: str,
        component_id: str,
        fields: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AddComponentResponse:
        """Add a component block to an existing device YAML."""
        return await add_component.add_component(
            self,
            configuration=configuration,
            component_id=component_id,
            fields=fields,
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
        """
        Validate a device YAML config. Streams output per-connection.

        ``show_secrets`` passes ``--show-secrets`` to ``esphome config``
        so resolved ``!secret`` values appear in the output instead of
        the default ``<removed>`` redaction. Default is ``False`` —
        secrets only appear when the user actively asks for them.
        Mirrors the legacy dashboard's ``streamer_mode`` semantics
        but as a per-call opt-in rather than a global setting, so one
        user wanting to see secrets in a multi-user deployment doesn't
        change the default for everyone else.
        """
        config_path = str(self._db.settings.rel_path(configuration))
        cmd = [*self._esphome_cmd, "--dashboard", "config", config_path]
        line_transform: Callable[[str], str] | None = None
        if show_secrets:
            cmd.append("--show-secrets")
        else:
            # ``esphome config`` without ``--show-secrets`` doesn't
            # redact — it wraps each ``password|key|psk|ssid`` value
            # in the ANSI conceal SGR (8/28). Browsers don't honour
            # that escape, so the resolved secret bytes were leaking
            # plain into the validate dialog. Strip the wrapped runs
            # before the line leaves the WS handler.
            line_transform = _redact_concealed_secrets
        await self._stream_subprocess(cmd, client, message_id, line_transform=line_transform)

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
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, atomic_write_file, path, content)

    async def _persist_yaml_mutation(self, configuration: str, content: str) -> None:
        """Atomic write + scan + StorageJSON regen for in-place YAML mutators."""
        await self._write_yaml_atomic_async(self._db.settings.rel_path(configuration), content)
        await self._scanner.scan()
        # Mirrors the upstream dashboard's
        # ``async_schedule_storage_json_update``; without it
        # ``loaded_integrations`` stays at its pre-write state.
        self._schedule_storage_regenerate(configuration)

    @staticmethod
    async def _read_yaml_async(path: Path) -> str:
        """Read *path* as UTF-8 text off the executor."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, path.read_text, "utf-8")

    def _on_scan_change(self, kind: ScanChange, device: Device) -> None:
        """Forward scanner changes onto the event bus."""
        event = {
            ScanChange.ADDED: EventType.DEVICE_ADDED,
            ScanChange.UPDATED: EventType.DEVICE_UPDATED,
            ScanChange.REMOVED: EventType.DEVICE_REMOVED,
        }[kind]
        self._db.bus.fire(event, DeviceEventData(device=device))
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
            # Drop reachability history for the gone device. Without
            # this, the four per-signal maps would accumulate one
            # entry per device that's ever lived in the catalog,
            # since nothing else clears them — the mDNS Removed
            # branch only fires when the device's broadcast goes
            # away, not when its YAML is deleted.
            self._reachability.clear(device.name)

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
                DeviceStateChangedData(
                    configuration=device.configuration,
                    state=state.value,
                ),
            )

    def _on_ip_change(self, name: str, ip: str, addresses: list[str]) -> None:
        """
        Forward IP updates onto the event bus and persist the primary value.

        ``ip=""`` (with an empty *addresses* list) means the device
        dropped off mDNS — we keep the last-known primary on disk so
        the OTA address cache stays warm across the device's offline
        window. The DNS pre-resolve and next mDNS resolve will
        overwrite it on reconnect.

        Only ``ip`` is persisted; ``addresses`` is the live mDNS view
        and gets repopulated by the next monitor pass after a restart.
        """
        new_addresses = list(addresses)
        for device in self._devices_by_name(name):
            if device.ip == ip and device.ip_addresses == new_addresses:
                continue
            ip_changed = device.ip != ip
            device.ip = ip
            device.ip_addresses = list(new_addresses)
            _LOGGER.debug(
                "Device %s (%s) IPs: %s",
                name,
                device.configuration,
                ", ".join(new_addresses) or "(cleared)",
            )
            if ip and ip_changed:
                self._db.create_background_task(
                    self._persist_device_ip_async(device.configuration, ip)
                )
            self._fire_device_updated(device)

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
            self._fire_device_updated(device)

    def _on_mac_address_change(self, name: str, mac: str) -> None:
        """
        Apply a MAC address observed via mDNS and derive interface MACs.

        The mDNS broadcast is always the device's primary MAC (Wi-Fi
        STA / eFuse base on ESP32, the single MAC on RP2040). When
        the YAML loads ``ethernet`` or any ``esp32_ble*`` /
        ``bluetooth_*`` integration we compute the corresponding
        interface MAC via :func:`derive_interface_macs` so the drawer
        can show every MAC the device owns without forcing the
        firmware to broadcast all of them.

        Persists ``mac_address`` to the per-device metadata sidecar
        so the dashboard shows the value immediately on restart —
        ESPHome devices stay mDNS-silent until probed. The derived
        MACs aren't persisted: they're deterministic from primary +
        ``loaded_integrations``, so a YAML edit that toggles
        bluetooth picks up the new derived MAC on the next reload
        without going through a stale-cache window. The early-return
        on equality skips both the in-memory write and the sidecar
        I/O on a steady-state announcement, keeping the typical
        "same value re-broadcast every 60s" cycle off-disk.
        """
        for device in self._devices_by_name(name):
            if device.mac_address == mac:
                continue
            device.mac_address = mac
            device.ethernet_mac, device.bluetooth_mac = derive_interface_macs(
                mac, device.target_platform, device.loaded_integrations
            )
            self._db.create_background_task(
                self._persist_device_metadata_async(device.configuration, mac_address=mac)
            )
            self._fire_device_updated(device)

    def _on_api_encryption_change(self, name: str, encryption: str) -> None:
        r"""
        Apply the API-encryption state observed via mDNS.

        Stores the broadcast value (or empty string for "TXT absent —
        device is plaintext") on the in-memory device. The dashboard's
        four-state lock indicator reads this together with
        ``api_encrypted`` to distinguish active / pending-flash /
        mismatch / plaintext.

        Also folds the wire signal into ``api_encrypted`` itself when
        a truthy cipher string arrives. ESPHome's compile pipeline
        runs the Jinja preprocessor over packages before YAML parsing
        (``api: |\n  # set ns = ...  ${ns.cfg}``); the dashboard's
        ``yaml_util.load_yaml`` doesn't, so the scan-time YAML pass
        can come back ``api_encrypted=False`` for a fully-encrypted
        device (issue #437). The live broadcast is the truthful
        signal — promoting ``api_encrypted`` here closes the gap for
        non-frontend consumers (HA integration, table-row menu
        gating, the ``Show API key`` affordance) that otherwise hide
        encryption-aware affordances on a YAML-detection miss. The
        symmetric "wire confirms plaintext" case (empty-string
        broadcast) deliberately doesn't *clear* ``api_encrypted`` —
        a wire-says-no while YAML-says-yes is the legitimate
        "mismatch" / "pending" shape the existing state machine
        already handles.
        """
        for device in self._devices_by_name(name):
            wire_promotes_encrypted = bool(encryption) and not device.api_encrypted
            if device.api_encryption_active == encryption and not wire_promotes_encrypted:
                continue
            device.api_encryption_active = encryption
            if wire_promotes_encrypted:
                device.api_encrypted = True
            self._fire_device_updated(device)

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
            self._fire_device_updated(device)

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

    def _sync_deployed_hash_after_flash(self, configuration: str) -> None:
        firmware_sync.sync_deployed_hash_after_flash(self, configuration)

    async def _persist_storage_version_async(self, configuration: str, version: str) -> None:
        await firmware_sync.persist_storage_version_async(self, configuration, version)

    @staticmethod
    def _persist_storage_version(configuration: str, version: str) -> None:
        """Write *version* to ``StorageJSON.esphome_version`` if it differs."""
        storage_path = resolve_storage_path(configuration)
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
