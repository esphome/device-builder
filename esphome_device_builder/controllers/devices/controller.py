"""
Devices controller — device CRUD, file watching, CLI operations, state management.

WS command surface plus the supporting state-monitor / scanner /
MQTT-coordinator glue. Pure data and free helpers live in
``constants.py`` and ``helpers.py``; the class itself lives here.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from esphome import const
from esphome.components.dashboard_import import import_config
from esphome.storage_json import StorageJSON, ext_storage_path, ignored_devices_storage_path

from ...helpers.api import CommandError, api_command
from ...helpers.build_size import coerce_sidecar_int
from ...helpers.config_hash import compute_yaml_config_hash, read_build_info_hash
from ...helpers.device_yaml import (
    generate_device_yaml,
    get_api_encryption_key,
    load_device_yaml,
    parse_esphome_meta,
    parse_platform_from_yaml,
)
from ...helpers.event_bus import Event, StreamControls, stream_events
from ...helpers.json import JSONDecodeError, dumps_indent, loads
from ...helpers.mac_addresses import derive_interface_macs
from ...helpers.process import kill_quietly
from ...helpers.subprocess import create_subprocess_exec, iter_lines_with_progress
from ...helpers.yaml import merge_component_yaml, rewrite_esphome_name
from ...models import (
    AddComponentResponse,
    AdoptableDevice,
    Device,
    DevicesResponse,
    DeviceState,
    ErrorCode,
    EventType,
    JobStatus,
    JobType,
    ReachabilitySource,
    StreamEvent,
    UpdateDeviceResponse,
    WizardResponse,
)
from .._build_size_refresher import BuildSizeRefresher
from .._device_mqtt_coordinator import DeviceMqttCoordinator
from .._device_scanner import DeviceFileMetadata, DeviceScanner, ScanChange
from .._device_state_monitor import _MDNS_REFRESH_PADDING_SECONDS, DeviceStateMonitor
from .._reachability_tracker import ReachabilityTracker
from ..config import (
    get_device_metadata,
    remove_device_metadata,
    set_device_labels,
    set_device_metadata,
)
from ..firmware.helpers import _find_esphome_cmd
from ._yaml_search import (
    DEFAULT_CONTEXT_LINES,
    MAX_CONTEXT_LINES,
    search_yaml_devices,
)
from ._yaml_search_cache import YamlSearchCache
from .helpers import (
    _apply_featured_presets,
    _archive_clear_device_sidecars,
    _build_address_cache_args,
    _drop_unconfigured_dependent_fields,
    _redact_concealed_secrets,
    _remove_device_sidecars,
    _validate_archive_configuration,
    _wipe_device_build_dir,
    friendly_name_slugify,
)

if TYPE_CHECKING:
    from ...device_builder import DeviceBuilder

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

# Per-file match cap for ``yaml/search``. Each device contributes
# at most this many lines so a chatty match (a query of ``:``
# against a deeply-nested config) doesn't drown hits in other
# devices. The dropdown caps its overall hit count at the
# caller-supplied ``max_results`` on top of this.
_YAML_SEARCH_PER_FILE_MATCH_CAP = 5


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
        target_name = configuration.removesuffix(".yaml").removesuffix(".yml")
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
        3. Neither → write a minimal stub the user fills in manually.

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
            storage_path = ext_storage_path(filename)
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

        # Reject same-name renames up-front. Both downstream branches
        # (manual rewrite, ``esphome rename``) would treat this as a
        # no-op rewrite + redundant flash — wasted work the caller
        # almost certainly didn't intend. Frontend should call
        # ``firmware/install`` for "flash without renaming".
        if new_filename == configuration:
            raise CommandError(
                ErrorCode.INVALID_ARGS,
                "new_name must differ from the current device name",
            )
        # Reject up-front if the target filename is already in use.
        # The manual-rename branch already checks ``new_path.exists()``
        # itself, but the CLI ``esphome rename`` path does *not* — it
        # blindly ``write_text``s the new YAML and then OTA-installs
        # it, so a collision would silently overwrite the unrelated
        # device's config and flash that firmware to the wrong device.
        # ``rel_path`` resolves the filename and ``.exists()`` is an
        # ``os.stat`` — both blocking syscalls. Push the pair to the
        # executor so the dashboard's request-path stays
        # event-loop-friendly on slow / network-mounted config dirs.
        loop = asyncio.get_running_loop()
        new_path = self._db.settings.rel_path(new_filename)
        if await loop.run_in_executor(None, new_path.exists):
            msg = f"A device named {new_filename} already exists"
            raise CommandError(ErrorCode.INVALID_ARGS, msg)

        if not await self._yaml_validates(config_path):
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
        """Run *action* per configuration; return one result dict each.

        Shared shape behind ``delete_bulk`` and ``archive_bulk``: each
        item in the returned list is ``{configuration, success}`` plus
        ``error`` (the exception's ``str``) on failure. A single
        ``_scanner.scan()`` runs after the whole batch — bulk teardown
        otherwise N-squares the bus traffic the dashboard subscribes to.
        """
        results: list[dict[str, Any]] = []
        for configuration in configurations:
            try:
                await action(configuration)
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
        * ``regen_failed_mtime`` + ``regen_failed_at`` in the
          metadata sidecar is the *cross-restart* version of the
          same skip. The previous backend stamped the YAML's
          mtime alongside ``time.time()``; a fresh start that
          finds those two intact and within
          ``_REGEN_FAILURE_TTL_SECONDS`` short-circuits without
          spawning another ``esphome compile`` on the same broken
          config. The check itself runs in an executor so the
          per-device ``stat()`` and metadata read don't stall the
          event loop on a fleet-wide cold start. Two retry
          signals release the guard:

          * The user edits the YAML — its mtime moves past the
            stamp, so the equality check fails naturally.
          * The TTL elapses — covers transient external problems
            (git package server flaky, DNS hiccup) where the
            user shouldn't have to touch the YAML to recover.
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
            # Last attempt this session failed and the YAML hasn't
            # changed since; rerunning would produce the same error.
            return

        async def _run() -> None:
            self._regenerate_pending.add(configuration)
            try:
                # Cross-restart skip: the previous backend persisted
                # the YAML's mtime + wall-clock when the regen
                # failed. If the file hasn't been touched since
                # *and* the failure stamp is still within the TTL,
                # replay would fail the same way — turn it into a
                # no-op. The check itself batches its disk reads
                # into one executor hop.
                if await self._regen_already_failed_recently_async(configuration):
                    self._regenerate_failed.add(configuration)
                    return
                async with self._regenerate_lock:
                    success = await self._spawn_only_generate(configuration)
                if success:
                    # ``--only-generate`` writes build_info.json
                    # with the canonical config_hash before
                    # exiting, same as a real compile. The single
                    # executor hop below reads that hash and
                    # writes the sidecar in one transaction, also
                    # clearing the regen-failure stamp now that
                    # the YAML generates cleanly.
                    await self._finalize_regen_success(configuration)
                    await self._scanner.reload(configuration)
                else:
                    self._regenerate_failed.add(configuration)
                    await self._stamp_regen_failure(configuration)
            finally:
                self._regenerate_pending.discard(configuration)

        self._db.create_background_task(_run())

    async def _spawn_only_generate(self, configuration: str) -> bool:
        """Run ``esphome compile --only-generate`` once. Return True iff exit-0.

        Both failure modes (spawn raised, or the subprocess exited
        non-zero) get logged at debug and produce ``False`` so the
        caller takes the same persist-failure-stamp branch in
        either case. Pulled out of ``_run()`` so the two failure
        paths don't have to duplicate the marker-set + persist
        sequence.
        """
        config_path = str(self._db.settings.rel_path(configuration))
        cmd = [*self._esphome_cmd, "--dashboard", "compile", "--only-generate", config_path]
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

    async def _regen_already_failed_recently_async(self, configuration: str) -> bool:
        """Return True iff the persisted failure stamp is unchanged-and-fresh.

        Both halves have to hold for the guard to fire:

        * The YAML's current ``stat.st_mtime`` equals the cached
          ``regen_failed_mtime`` — same file as last time (any
          edit moves the mtime forward).
        * Less than ``_REGEN_FAILURE_TTL_SECONDS`` has elapsed
          since the cached ``regen_failed_at`` — covers transient
          external causes (git package server, DNS, ESPHome
          mid-flight) by allowing a re-check after the TTL.

        Disk reads (``Path.stat``, the ``.device-builder.json``
        parse) batch into a single executor job so a cold-start
        fleet sweep neither stalls the event loop nor double-books
        the default thread pool. A negative age (clock skew, NTP
        step, future-dated stamp) clamps to zero; without that
        clamp a bad sidecar value could lock out the regen
        indefinitely.
        """
        loop = asyncio.get_running_loop()
        config_dir = self._db.settings.config_dir
        config_path = self._db.settings.rel_path(configuration)

        def _read() -> tuple[float, dict[str, Any]] | None:
            # One executor hop for both reads — paying for two
            # parallel ``run_in_executor`` jobs would just consume
            # two slots in the shared default thread pool for work
            # that's already serial on disk anyway.
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

    async def _stamp_regen_failure(self, configuration: str) -> None:
        """Persist the cross-restart "we already tried, gave up" marker — one executor hop.

        Combines the YAML ``stat()`` and the sidecar write into a
        single closure handed to ``run_in_executor``. The earlier
        standalone-stamp shape took two hops (one to stat, one to
        write); on a fleet-wide cold-start each saved hop is a
        thread-pool slot back to the pool.

        The wall-clock half is sampled inside the closure too, so
        the stamp captures the same instant the file's mtime was
        observed instead of straddling a hop.
        """
        config_dir = self._db.settings.config_dir
        config_path = self._db.settings.rel_path(configuration)

        def _stamp() -> None:
            try:
                mtime = config_path.stat().st_mtime
            except OSError:
                return  # file vanished mid-regen; nothing useful to stamp
            set_device_metadata(
                config_dir,
                configuration,
                regen_failed_mtime=mtime,
                regen_failed_at=time.time(),
            )

        await asyncio.get_running_loop().run_in_executor(None, _stamp)

    async def _finalize_regen_success(self, configuration: str) -> None:
        """Read the post-only-generate hash and clear the failure stamp — one executor hop.

        Used to be three separate awaits — read ``build_info.json``,
        write the hash, write the cleared regen stamp — totalling
        three executor hops and two sidecar transactions. The
        closure here folds them together: one ``read_build_info_hash``
        call, one ``set_device_metadata`` transaction that writes
        ``expected_config_hash`` and clears
        ``regen_failed_mtime`` / ``regen_failed_at`` atomically.

        See :meth:`_persist_expected_config_hash` for the rationale
        on why the hash is read off ``build_info.json`` rather than
        recomputed in-process — a missing / malformed file is
        unexpected on this code path so the warning log lives there.
        """
        config_dir = self._db.settings.config_dir
        yaml_path = self._db.settings.rel_path(configuration)

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
                "Could not read config_hash from build_info.json for %s — "
                "the drawer's Local hash may stay stale until the next flash. "
                "If this persists across compiles, check that ESPHome's "
                "build_info.json schema hasn't changed.",
                configuration,
            )
            return
        _LOGGER.debug("Stored expected_config_hash for %s: %s", configuration, new_hash)

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
            # The frontend's catalog-derived id suggestion for featured
            # components is the dashed ``featured_<board>_<local>``
            # form (e.g. ``featured_athom-smart-plug-v3_power_monitor_1``
            # — the board id still carries dashes), which ESPHome rejects.
            # Reset to empty when the supplied id contains a dash so
            # ``generate_component_yaml`` produces a valid auto-id from
            # the underlying component + name — a user-typed custom id
            # without dashes passes through.
            user_id = fields.get("id")
            if isinstance(user_id, str) and "-" in user_id:
                fields["id"] = ""

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
        # Honour each field's ``depends_on_component`` gate against
        # what's actually in the device YAML — drops MQTT-only options
        # (``availability:``, ``state_topic:``, ...) when the device
        # has no ``mqtt:`` block, mirroring what the frontend already
        # does field-by-field on the input form.
        fields = _drop_unconfigured_dependent_fields(fields, component, existing)
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
        # Honour the network type the discovery TXT advertised — an
        # ESP32-PoE / Olimex / etc. broadcasts ``network=ethernet``
        # and the imported template needs to start from
        # ``ethernet:`` rather than the Wi-Fi default.
        #
        # Prefer the direct ``name`` → ``import_result`` lookup since
        # factory firmware broadcasts with a MAC suffix
        # (``apollo-plt-1-983300``), which keeps each entry unique
        # per physical device even when multiple identical products
        # share the same ``package_import_url``. The frontend
        # pre-fills the adoption dialog with the discovery row's
        # broadcast name, so this matches in the common path.
        # Fall back to a ``package_import_url`` match only when the
        # user edited the name during adoption — at that point the
        # ``import_result`` key no longer matches. The fallback is
        # technically ambiguous between identical-product devices,
        # but those share the same ``network`` value so picking
        # whichever lands first is correct in practice.
        # Final fallback to Wi-Fi when no row matches at all (older
        # factory firmware that didn't advertise the field, or a
        # discovery row that was already purged).
        adoptable = self.import_result.get(name) or next(
            (d for d in self.import_result.values() if d.package_import_url == package_import_url),
            None,
        )
        network = adoptable.network if adoptable and adoptable.network else const.CONF_WIFI
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
                network,
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
            self._state_monitor.apply_ip_addresses(name, cached)
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

        Wire shape:
          → ``{"command": "devices/subscribe_reachability",
                "message_id": "<id>",
                "args": {"device_name": "kitchen"}}``
          ← ``{"event": "reachability_state", "message_id": "<id>",
                "data": <ReachabilitySnapshot>}``  (initial + on every change)
          ← ``{"result": {"subscribed": true}, "message_id": "<id>"}``
          → ``{"command": "devices/stop_stream",
                "args": {"stream_id": "<id>"}}``  (to end the stream)

        While subscribed AND the device's active source is mDNS,
        the backend force-refreshes the A record every 60s so a
        stale broadcast doesn't keep the displayed "last seen" age
        growing forever. Ping-source devices are already covered by
        the regular ping sweep; MQTT-source by the discover-publish
        loop. Both feed the tracker through the same path the
        initial subscription read.
        """
        if client is None:
            return
        if not device_name:
            raise CommandError(ErrorCode.INVALID_MESSAGE, "device_name is required")
        if self.get_reachability_snapshot(device_name) is None:
            raise CommandError(ErrorCode.NOT_FOUND, f"No configured device named {device_name!r}")

        # Register so a peer ``devices/stop_stream`` (or this client's
        # cleanup on disconnect) cancels the running task.
        task = asyncio.current_task()
        assert task is not None
        client.register_stream(message_id, task)

        refresh_task: asyncio.Task | None = None

        async def _send_initial(controls: StreamControls) -> None:
            snapshot = self.get_reachability_snapshot(device_name)
            if snapshot is not None:
                await client.send_event(message_id, "reachability_state", snapshot)
            await client.send_result(message_id, {"subscribed": True})

        def _handle_event(event: Event, controls: StreamControls) -> None:
            data = event.data
            if data.get("device") != device_name:
                # The bus event is broadcast (one listener for every
                # subscriber); filter inside the closure so each
                # subscriber only forwards the events for its device.
                return
            controls.push("reachability_state", data)

        try:
            # Spawn the 60s mDNS refresh loop alongside the stream
            # so it gets cancelled together with the subscription
            # when the WS disconnects or ``devices/stop_stream``
            # cancels this task.
            refresh_task = asyncio.create_task(self._reachability_refresh_loop(device_name))
            await stream_events(
                client=client,
                message_id=message_id,
                bus=self._db.bus,
                event_types=[EventType.DEVICE_REACHABILITY],
                handle_event=_handle_event,
                send_initial=_send_initial,
            )
        finally:
            if refresh_task is not None:
                refresh_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await refresh_task
            client.unregister_stream(message_id)

    async def _reachability_refresh_loop(self, device_name: str) -> None:
        """Schedule mDNS refreshes off the cached A record's expiry.

        Quiet when active source is ping (the regular sweep already
        runs every 60s) or MQTT (the discover-publish loop already
        ticks every 2s).

        Why scheduled-on-expiry rather than fixed-interval: the
        canonical ``async_resolve_host`` short-circuits on cache
        hit (``_load_from_cache`` returns the cached value if
        the record is present and not expired), so a
        fixed-interval probe within the cache's lifetime
        wouldn't actually go on the wire — we'd just keep
        re-reading the same cached entry until it eventually
        ages out and the next iteration finally reaches
        ``async_request``.

        On every iteration, re-read the cached A record's
        remaining TTL. If a fresh entry is alive, sleep until it
        ages out (``ttl_remaining + padding``) then loop —
        rechecking after the sleep handles the case where an
        unrelated mDNS announce reached us during the sleep
        window and re-armed the cache; we just sleep again for
        the new lifetime instead of issuing a redundant query.
        Only when the recheck shows expired / absent does the
        wire query fire — by then ``_load_from_cache`` will fail
        and ``async_resolve_host`` will actually go on the wire.
        ESPHome devices are mDNS-silent except in response to
        probes; ``ServiceBrowser`` only keeps the PTR record
        (4500s TTL) alive, not A/AAAA (120s). Without this loop
        the A record decays unrecoverably 120s after the most
        recent probe.
        """
        while True:
            # Use the A/AAAA-specific TTL — not the union-of-types
            # ``get_mdns_cache_info``: PTR has a 4500s TTL and
            # stays cached for ages, so a sleep keyed on it
            # would never wake up to refresh A. We're driving
            # the loop off the A record's much shorter 120s
            # decay because that's the one we actually need to
            # keep alive for the drawer's freshness display.
            a_ttl_remaining = self._state_monitor.get_mdns_a_record_ttl_remaining(device_name)
            if a_ttl_remaining is not None and a_ttl_remaining > 0:
                # A still alive — sleep until just past expiry,
                # then re-check rather than probing immediately.
                # A fresh announce arriving during the sleep
                # would re-arm the cache and the recheck spares
                # us a redundant wire query.
                await asyncio.sleep(a_ttl_remaining + _MDNS_REFRESH_PADDING_SECONDS)
                continue
            # A expired or absent — probe the wire to refresh
            # it. The padding before the first probe also gives
            # the subscription's initial snapshot a chance to
            # land before we issue our first query.
            await asyncio.sleep(_MDNS_REFRESH_PADDING_SECONDS)
            if self._state_monitor.priority_for(device_name) is ReachabilitySource.MDNS:
                await self.refresh_device_mdns(device_name)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_devices(self) -> list[Device]:
        """Bridge for the state monitor (``self._scanner.devices`` is a property)."""
        return self._scanner.devices

    def _resolve_device_metadata(self, config_dir: Path, filename: str) -> DeviceFileMetadata:
        """
        Resolve a device's persisted ``board_id`` / ``ip`` / config hash / MAC.

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

        ``mac_address`` is the canonical ``XX:XX:XX:XX:XX:XX`` form
        last observed on the device's mDNS ``mac`` TXT, persisted
        to the sidecar so the dashboard renders the value
        immediately on restart (ESPHome devices are mDNS-silent
        until probed). Empty when the device hasn't been seen yet
        — the next mDNS announcement repopulates via
        :meth:`_on_mac_address_change`. The derived
        ``ethernet_mac`` / ``bluetooth_mac`` are recomputed by
        :func:`derive_interface_macs` at ``Device`` construction
        time, not stored in the sidecar.
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
        mac_address = str(md.get("mac_address", ""))
        # ``coerce_sidecar_int`` handles the bad-data fall-throughs
        # (``None`` / object / decimal-string / etc.) — same
        # defensive shape used by the build-size cache reads in
        # ``helpers/build_size.py``. The metadata resolver is on
        # the scanner's per-device hot path; a single corrupt
        # entry shouldn't fail the whole scan.
        build_size_bytes = coerce_sidecar_int(md.get("build_size_bytes"))
        # Defensive filter: a hand-edited sidecar could land non-string
        # entries in the labels list. The scanner is on the hot path,
        # so silently drop bad entries rather than failing the whole
        # device load.
        raw_labels = md.get("labels")
        labels: tuple[str, ...]
        if isinstance(raw_labels, list):
            labels = tuple(item for item in raw_labels if isinstance(item, str))
        else:
            labels = ()
        return DeviceFileMetadata(
            board_id=board_id,
            ip=ip,
            expected_config_hash=expected_config_hash,
            mac_address=mac_address,
            build_size_bytes=build_size_bytes,
            labels=labels,
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

    def _build_reachability_snapshot(self, name: str) -> dict[str, object] | None:
        """
        Stitch state + tracker fields into the reachability wire shape.

        The state monitor owns ``state`` / ``active_source`` / ``ip``;
        the tracker owns the per-signal freshness fields. Both
        ``get_reachability_snapshot`` (initial WS subscribe) and
        ``_on_reachability_observation`` (per-event push) need the
        merged dict, so the device-lookup + delegate-to-tracker
        combo lives once here. Returns ``None`` when no configured
        device matches *name*.
        """
        bucket = self._scanner.get_by_name(name)
        if not bucket:
            return None
        first = bucket[0]
        return self._reachability.snapshot(
            name,
            state=first.state,
            active_source=self._state_monitor.priority_for(name),
            ip=first.ip,
        )

    def _on_reachability_observation(self, name: str) -> None:
        """
        Forward a reachability freshness observation onto the event bus.

        Fires :data:`EventType.DEVICE_REACHABILITY` carrying the full
        wire-shape snapshot for *name*. The device drawer's per-device
        subscription filters by ``data["device"]`` and pushes the
        snapshot to the client. The event is *not* forwarded by the
        broadcast ``subscribe_events`` channel — adding a periodic
        per-device freshness ping to every connected client would
        bloat the bus for no UI gain.
        """
        snapshot = self._build_reachability_snapshot(name)
        if snapshot is None:
            return
        self._db.bus.fire(EventType.DEVICE_REACHABILITY, snapshot)

    def get_reachability_snapshot(self, name: str) -> dict[str, object] | None:
        """Return the current reachability snapshot for *name*, or ``None``.

        Public so the WS ``devices/subscribe_reachability`` handler can
        seed its initial event without going through the bus. Returns
        ``None`` when no configured device matches *name* (the
        subscription handler maps that to a NOT_FOUND error).
        """
        return self._build_reachability_snapshot(name)

    async def refresh_device_mdns(self, name: str) -> None:
        """Force-refresh a device's mDNS A record. No-op if zeroconf is down."""
        await self._state_monitor.refresh_mdns(name)

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
            self._db.bus.fire(EventType.DEVICE_UPDATED, {"device": device})

    async def _persist_device_ip_async(self, configuration: str, ip: str) -> None:
        """Save *ip* to the device-builder metadata sidecar."""
        await self._persist_device_metadata_async(configuration, ip=ip)

    async def _persist_device_metadata_async(self, configuration: str, **fields: Any) -> None:
        """
        Run a blocking ``set_device_metadata`` write on the default executor.

        Centralises the ``loop.run_in_executor(None, lambda: set_device_metadata(
        config_dir, configuration, **fields))`` boilerplate that every
        async-context sidecar write was repeating. Callers pass the
        same kwargs they'd hand directly to
        ``controllers.config.set_device_metadata``; the helper takes
        care of the loop lookup and the ``config_dir`` resolution
        from the device builder's settings.

        Stays a method (not a free function) because every call site
        already needs ``self._db`` to reach the loop and config_dir
        — pulling it out to the module level would make every caller
        thread the same two values explicitly with no readability
        win.
        """
        loop = asyncio.get_running_loop()
        config_dir = self._db.settings.config_dir
        await loop.run_in_executor(
            None, lambda: set_device_metadata(config_dir, configuration, **fields)
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
        configuration = getattr(job, "configuration", "")
        if not configuration:
            return
        if job_type == JobType.CLEAN:
            # ``esphome clean`` removes the per-device build tree;
            # the build-size cache for this device is now stale
            # (cached non-zero, current dir mtime → 0). The pair-
            # equality short-circuit in
            # ``refresh_build_size_if_stale`` detects that and
            # walks once to clear the cached triple, so the drawer
            # / table flip back to the em-dash placeholder. No
            # hash recompute / flash bookkeeping needed for CLEAN.
            self._build_size.request(configuration)
            return
        if job_type not in (JobType.COMPILE, JobType.UPLOAD, JobType.INSTALL):
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
        # A real compile moves the freshness pair the build-size
        # cache keys off (build-dir mtime + ``build_info.json``
        # mtime); hand off to the build-size worker so the drawer
        # / table show an up-to-date "Build size" value the next
        # time the frontend reads the device list. The worker
        # short-circuits when the pair didn't actually move (e.g.
        # an UPLOAD-only job that didn't recompile).
        self._build_size.request(configuration)

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
        await self._persist_device_metadata_async(configuration, expected_config_hash=new_hash)
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

    async def _archive_single(self, configuration: str) -> None:
        """Soft-delete: move the YAML into ``<config_dir>/archive/`` and wipe build artifacts.

        Mirrors the legacy dashboard's archive flow with one
        deliberate divergence: we also wipe the StorageJSON
        sidecar (a pure build artifact — ``firmware_bin_path`` /
        ``loaded_integrations`` / ``target_platform`` go stale
        the moment the build dir is removed). The legacy dashboard
        preserved StorageJSON so unarchive could restore cached
        IP / version, but ours uses ``ext_storage_path`` which
        is per-filename keyed — a future same-name configuration
        would inherit the archived device's stale build state
        until recompiled. Wiping on archive trades a few seconds
        of "unknown state" after unarchive (the scanner + monitor
        refill from the next mDNS broadcast + the next compile)
        for full isolation against same-name new devices.

        The device-metadata sidecar is treated more carefully —
        only volatile fields (``ip``, ``expected_config_hash``)
        are cleared. Stable identity fields (``board_id``,
        ``friendly_name``, ``comment``) survive so an unarchive
        of the same YAML restores the user-visible state
        unchanged. ``board_id`` in particular is the catalog →
        YAML match key; an earlier iteration wiped the entire
        entry and forced a re-derive on every archive →
        unarchive cycle. See
        ``_archive_clear_device_sidecars`` for the keep / clear
        rationale.

        Build dir wipe matches what ``_delete_single`` does — an
        archived device's compile output is dead weight (the
        user can recompile after unarchive). The YAML itself
        stays on disk so the operation is reversible.
        """
        config_path = self._db.settings.rel_path(configuration)
        loop = asyncio.get_running_loop()
        config_dir = self._db.settings.config_dir

        def _archive_sync() -> None:
            if not config_path.exists():
                msg = f"File not found: {configuration}"
                raise FileNotFoundError(msg)
            archive_dir = config_dir / "archive"
            archive_dir.mkdir(parents=True, exist_ok=True)
            target = archive_dir / configuration
            if target.exists():
                # Same name already archived. We can't silently rename
                # to ``<name> (2).yaml`` because the StorageJSON sidecar
                # and metadata stay keyed on the original filename —
                # a later unarchive of the suffixed copy would surface
                # without its sidecar and lose the cached address /
                # version / loaded_integrations. Refuse the operation
                # and let the user resolve the collision explicitly
                # (unarchive the existing copy or delete it).
                msg = (
                    f"Cannot archive {configuration}: an archived config "
                    "with the same name already exists. Unarchive or "
                    "permanently delete the existing archive first."
                )
                raise FileExistsError(msg)
            # Wipe build dir first (same shape as delete), then
            # move the YAML, then clear the build-artifact
            # sidecars while keeping stable identity fields so an
            # unarchive of this same YAML restores its
            # user-visible state. See the docstring for the
            # keep / clear split.
            _wipe_device_build_dir(configuration)
            shutil.move(str(config_path), str(target))
            _archive_clear_device_sidecars(config_dir, configuration)

        try:
            await loop.run_in_executor(None, _archive_sync)
        except FileExistsError as exc:
            raise CommandError(ErrorCode.INVALID_ARGS, str(exc)) from exc

    async def _unarchive_single(self, configuration: str) -> None:
        """Move an archived YAML back into the active config_dir.

        Refuses to clobber an existing active YAML — that case
        means the user already created a new device under the same
        filename, and silently overwriting it would surprise them.
        Surface a ``CommandError`` instead so the dialog can prompt
        for a different action.
        """
        loop = asyncio.get_running_loop()
        config_dir = self._db.settings.config_dir
        archive_path = config_dir / "archive" / configuration
        target = self._db.settings.rel_path(configuration)

        def _unarchive_sync() -> None:
            if not archive_path.exists():
                msg = f"Archived file not found: {configuration}"
                raise FileNotFoundError(msg)
            if target.exists():
                msg = (
                    f"Cannot unarchive {configuration}: an active config "
                    f"with the same name already exists"
                )
                raise FileExistsError(msg)
            shutil.move(str(archive_path), str(target))

        try:
            await loop.run_in_executor(None, _unarchive_sync)
        except FileExistsError as exc:
            raise CommandError(ErrorCode.INVALID_ARGS, str(exc)) from exc

    def _list_archived_sync(self) -> list[dict[str, Any]]:
        """Read ``<config_dir>/archive/`` and parse each YAML's meta block.

        Returns one dict per archived YAML with the same name /
        friendly_name / comment fields the active device list
        carries, plus ``configuration`` so the dashboard can
        address each entry. Files that don't parse are skipped
        with a debug log — the archive dir is user-managed and
        a stray non-YAML file shouldn't crash the listing.

        When the YAML's ``esphome:`` block is sparse (e.g. friendly
        name only ever lived in StorageJSON because the user wrote
        it via the dashboard's edit dialog rather than the YAML),
        fall back to the StorageJSON sidecar before degrading to
        the bare filename. ``_archive_single`` wipes its own
        sidecars on archive, so the fallback only matters for
        legacy archives (created by the upstream ESPHome dashboard
        or by an earlier version of this server before the sidecar
        wipe landed) and for entries dropped into the archive dir
        externally.
        """
        archive_dir = self._db.settings.config_dir / "archive"
        if not archive_dir.is_dir():
            return []
        results: list[dict[str, Any]] = []
        for path in sorted(archive_dir.iterdir()):
            if path.suffix not in (".yaml", ".yml") or path.name.startswith("."):
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                _LOGGER.debug("Failed to read archived YAML %s", path, exc_info=True)
                continue
            name, friendly_name, comment, _ = parse_esphome_meta(content)
            if not name or not friendly_name or comment is None:
                storage = StorageJSON.load(ext_storage_path(path.name))
                if storage is not None:
                    name = name or storage.name
                    friendly_name = friendly_name or storage.friendly_name
                    if comment is None:
                        comment = storage.comment
            results.append(
                {
                    "configuration": path.name,
                    "name": name or path.stem,
                    "friendly_name": friendly_name or name or path.stem,
                    "comment": comment,
                }
            )
        return results

    async def _delete_archived_single(self, configuration: str) -> None:
        """Permanently remove an archived YAML and its sidecars.

        Mirrors ``_delete_single`` but operates on
        ``<config_dir>/archive/<configuration>`` instead of the
        active config_dir. The build dir is already gone (archive
        wipes it), so this only has to remove the YAML, the
        StorageJSON sidecar, and the device-metadata sidecar.

        Defense-in-depth: the StorageJSON / metadata sidecars are
        keyed on the bare filename, so if an active config of the
        same name has been re-created since the archive, those
        sidecars belong to the live device and removing them
        would wipe its cached IP / hash / loaded_integrations.
        ``_archive_single`` already wipes its own sidecars on the
        way in (so this collision shouldn't happen in practice),
        but we still guard with an existence check on the active
        path. Callers expect best-effort cleanup of orphan
        sidecars, not a guarantee of their removal.
        """
        loop = asyncio.get_running_loop()
        config_dir = self._db.settings.config_dir
        archive_path = config_dir / "archive" / configuration
        active_path = self._db.settings.rel_path(configuration)

        def _delete_all() -> None:
            if not archive_path.exists():
                msg = f"Archived file not found: {configuration}"
                raise FileNotFoundError(msg)
            archive_path.unlink()
            if active_path.exists():
                # An active config with the same filename owns the
                # sidecars now — leave them alone.
                return
            _remove_device_sidecars(config_dir, configuration)

        await loop.run_in_executor(None, _delete_all)

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
            # Wipe build dir first so a partial failure later still
            # leaves the user able to retry the delete.
            _wipe_device_build_dir(configuration)
            config_path.unlink(missing_ok=True)
            (config_dir / ".trash" / configuration).unlink(missing_ok=True)
            (config_dir / ".archive" / f"{configuration}.json").unlink(missing_ok=True)
            _remove_device_sidecars(config_dir, configuration)

        await loop.run_in_executor(None, _delete_all)

    async def _stream_subprocess(
        self,
        cmd: list[str],
        client: Any,
        message_id: str,
        *,
        line_transform: Callable[[str], str] | None = None,
    ) -> None:
        """Run a CLI subprocess and stream its merged stdout/stderr to a single client.

        Registers the running task with the client so a peer ``devices/stop_stream``
        command (or a WS disconnect) can cancel it; cancellation kills the
        subprocess so it doesn't keep running detached.

        ``line_transform``, if given, is applied to every output line
        before it leaves the WS handler. Used by ``validate_config``
        to scrub the resolved ``!secret`` values out of the stream
        when ``show_secrets`` is off (``esphome config`` doesn't
        actually redact in that mode — it wraps values with the ANSI
        conceal SGR, which browsers don't honour).
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
            # Use the shared `\n`/`\r` splitter so esptool / PlatformIO
            # carriage-return progress lines surface live instead of
            # buffering until the next newline. Strip the terminator
            # from each event payload — the frontend's logs view
            # appends every event as a new line, unlike the firmware
            # job-output path which preserves terminators for in-place
            # overwrites.
            async for line in iter_lines_with_progress(proc.stdout):
                payload = line.rstrip("\n\r")
                if line_transform is not None:
                    payload = line_transform(payload)
                await client.send_event(message_id, StreamEvent.OUTPUT, payload)
            exit_code = await proc.wait()
        except asyncio.CancelledError:
            # Synchronous kill only — no awaits in the cancel path. The
            # ``finally`` block reaps the process and ``devices/stop_stream``
            # is what tells the frontend the cancel succeeded. ``proc`` may
            # be ``None`` if cancellation arrived before spawn returned.
            if proc is not None and proc.returncode is None:
                kill_quietly(proc)
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
