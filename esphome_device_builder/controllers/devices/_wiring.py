"""
Collaborator construction for :class:`DevicesController`.

Builds the scanner / state-monitor / reachability-tracker / MQTT-coordinator
/ metadata-store graph and attaches each handle to the controller. Kept out
of the controller body so the wiring order (and the comments justifying it)
don't dominate the file. Order is load-bearing — see the inline notes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from esphome.core import CORE

from .._build_size_refresher import BuildSizeRefresher
from .._device_mqtt_coordinator import DeviceMqttCoordinator
from .._device_scanner import DeviceScanner
from .._device_state_monitor import DeviceStateMonitor
from .._reachability_tracker import ReachabilityTracker
from ._metadata_store import DeviceMetadataStore
from ._shared_sidecar import SharedSidecarClient
from ._yaml_search_cache import YamlSearchCache

if TYPE_CHECKING:
    from .controller import DevicesController


def wire_collaborators(controller: DevicesController) -> None:
    """Construct and attach the controller's background collaborators."""
    # Constructed before the scanner so the first
    # ``_resolve_device_metadata`` reads off the store.
    controller._shutdown_callbacks = []
    controller._metadata_store = DeviceMetadataStore(
        config_dir=controller._db.settings.config_dir,
        data_dir=Path(CORE.data_dir),
        shutdown_register=controller._shutdown_callbacks.append,
    )
    controller._shared_sidecar = SharedSidecarClient(controller._db.settings.config_dir)

    # Per-file locks serialising a YAML write with its version-history
    # commit (see ``_persist_yaml_mutation``). One ``asyncio.Lock`` per
    # distinct filename written this process lifetime; never evicted —
    # popping on delete could desync a lock a concurrent save is
    # awaiting. Negligible in practice (a real fleet reuses filenames);
    # only churn through many unique names grows it.
    controller._yaml_write_locks = {}

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
    controller._regenerate_lock = asyncio.Lock()

    # ``yaml/search`` per-file cache. The class owns its own
    # ``stat``-then-read flow + ``asyncio.Lock`` so the
    # bookkeeping doesn't sprawl across this controller. See
    # ``_yaml_search_cache.YamlSearchCache``.
    controller._yaml_search_cache = YamlSearchCache()
    # Global search lock — ``yaml/search`` is I/O-bound (one
    # ``stat`` per device + reads on cache misses), so two
    # concurrent searches against the same fleet would just
    # double the disk pressure without helping latency. Serialise
    # to one in-flight call per controller; the frontend's
    # debounce + concurrency-of-1 gate keeps the queue depth low
    # in normal use, and a slow request from a stuck client
    # won't fan out to N parallel walks.
    controller._yaml_search_lock = asyncio.Lock()

    controller._scanner = DeviceScanner(
        config_dir=controller._db.settings.config_dir,
        get_metadata=controller._resolve_device_metadata,
        on_change=controller._on_scan_change,
    )
    # Single-worker build-size refresher. Bulk operations
    # (clean / delete N devices in a row, fleet-wide startup
    # sweep) all funnel into one queue so repeated requests
    # for the same configuration coalesce and we never pile
    # up background tasks.
    controller._build_size = BuildSizeRefresher(
        get_filenames=lambda: (d.configuration for d in controller._get_devices()),
        get_metadata_snapshot=controller._metadata_store.snapshot_all,
        persist_size=controller._persist_build_size,
        on_refreshed=controller._scanner.reload,
    )
    # Build the state monitor first so the reachability tracker
    # can take its ``get_mdns_cache_info`` bound method directly
    # as the mDNS cache reader (no wrapper lambda — bound
    # methods already match the ``Callable[[str], MdnsCacheInfo
    # | None]`` shape). Wire the tracker back onto the monitor
    # after construction; the monitor only invokes
    # ``self._reachability`` at observation time so the
    # initial ``None`` is fine.
    controller._state_monitor = DeviceStateMonitor(
        get_devices=controller._get_devices,
        get_devices_by_name=controller._scanner.get_by_name,
        on_state_change=controller._on_state_change,
        on_ip_change=controller._on_ip_change,
        on_version_change=controller._on_version_change,
        on_config_hash_change=controller._on_config_hash_change,
        on_api_encryption_change=controller._on_api_encryption_change,
        on_mac_address_change=controller._on_mac_address_change,
        on_importable_added=controller._on_importable_added,
        on_importable_removed=controller._on_importable_removed,
        is_ignored=controller.state.ignored_devices.__contains__,
        presence=controller._db.subscriber_presence,
    )
    # Per-signal freshness tracker (mDNS / ping / MQTT last-seen,
    # ping RTT) feeding the device drawer's Reachability section.
    # Lives here on the controller so the subscribe handler can
    # call ``snapshot()`` on demand; observations come in via the
    # state monitor.
    controller._reachability = ReachabilityTracker(
        on_observation=controller._on_reachability_observation,
        mdns_cache_reader=controller._state_monitor.get_mdns_cache_info,
    )
    controller._state_monitor.set_reachability(controller._reachability)
    # MQTT routes its observations through the same state monitor so
    # source-priority is enforced in one place.
    controller._mqtt_coordinator = DeviceMqttCoordinator(
        config_dir=controller._db.settings.config_dir,
        get_devices=controller._get_devices,
        on_state_change=lambda n, s: controller._state_monitor.apply(n, s, "mqtt"),
        on_ip_change=controller._state_monitor.apply_ip,
    )
