"""Scan-change orchestrator for ``DevicesController``."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ...models import Device, DeviceEventData, EventType
from .._device_scanner import ScanChange

if TYPE_CHECKING:
    from .controller import DevicesController

_LOGGER = logging.getLogger(__name__)


def on_scan_change(
    controller: DevicesController,
    kind: ScanChange,
    device: Device,
    previous: Device | None,
) -> None:
    """Forward scanner changes onto the event bus and fan out per-kind side effects."""
    # UPDATED and RELOADED both refresh the client row via DEVICE_UPDATED;
    # only UPDATED (the scanner saw the YAML's mtime/size/inode change) also
    # fires DEVICE_YAML_UPDATED, so version history commits on edits but not
    # on metadata reloads.
    event = {
        ScanChange.ADDED: EventType.DEVICE_ADDED,
        ScanChange.UPDATED: EventType.DEVICE_UPDATED,
        ScanChange.RELOADED: EventType.DEVICE_UPDATED,
        ScanChange.REMOVED: EventType.DEVICE_REMOVED,
    }[kind]
    payload = DeviceEventData(device=device)
    controller._db.bus.fire(event, payload)
    if kind is ScanChange.UPDATED:
        controller._db.bus.fire(EventType.DEVICE_YAML_UPDATED, payload)
    if kind is ScanChange.ADDED:
        # ``probe_device`` short-circuits to the zeroconf cache
        # when present; otherwise it spawns a fire-and-forget
        # resolve task. Without this, YAMLs dropped on disk
        # outside the API entrypoints (git clone, copy from
        # another dashboard) sit at "Unknown" until the next
        # periodic ping sweep.
        controller._state_monitor.probe_device(device.name)
        # Paired ICMP probe covers ping-only devices that don't
        # broadcast ``_esphomelib._tcp``; a cold-start herd of wakes
        # is absorbed into the first post-bootstrap sweep.
        controller._state_monitor.probe_device_ping(device.name)
        # Drop the stale importable row so connected subscribe_events
        # clients stop showing the adopt banner once the device is
        # configured. Idempotent: fires REMOVED only if a row existed.
        controller._on_importable_removed(device.name)
    if (
        kind in (ScanChange.UPDATED, ScanChange.RELOADED)
        and previous is not None
        and previous.address != device.address
    ):
        # The change swapped in a new address (a ``wifi.use_address``
        # edit, or the post-regen StorageJSON replacing the
        # ``<file>.local`` fallback); without the wake the new address
        # waits out the remainder of the periodic sweep interval.
        controller._state_monitor.probe_device_ping(device.name)
    if kind in (ScanChange.UPDATED, ScanChange.RELOADED, ScanChange.REMOVED):
        # YAML cache key changed (or a reload re-read it); clear any
        # prior failure marker so the next edit gets a fresh chance at
        # ``--only-generate`` (and re-creating a deleted file
        # later doesn't inherit the old failure).
        controller.state.regenerate_failed.discard(device.configuration)
    # First-sight devices with no compile output carry the
    # ``<filename>.local`` address fallback and an empty
    # ``loaded_integrations`` list. Schedule a background
    # ``--only-generate`` so the next scan picks up the real
    # StorageJSON-derived values without making the user wait
    # for a real compile. Also fire when ``expected_config_hash``
    # is empty even though ``loaded_integrations`` is populated:
    # devices configured before build_info.json existed have a
    # working StorageJSON but no hash, and would otherwise show
    # a permanent em-dash for "Local config hash" until the user
    # edits the YAML.
    needs_storage_regen = kind is ScanChange.ADDED and (
        not device.loaded_integrations or not device.expected_config_hash
    )
    if needs_storage_regen:
        missing = []
        if not device.loaded_integrations:
            missing.append("loaded_integrations")
        if not device.expected_config_hash:
            missing.append("expected_config_hash")
        _LOGGER.debug(
            "Scheduling --only-generate for %s (missing: %s)",
            device.configuration,
            ", ".join(missing),
        )
        controller._schedule_storage_regenerate(device.configuration)
    if kind is ScanChange.REMOVED:
        # Upstream's DashboardImportDiscovery only fires
        # on_update on first sight; without a nudge a deleted
        # device's discovery row stays silent until the device
        # re-announces (potentially many minutes for a quiet
        # one). The "revisit all" variant covers the case where
        # the user adopted with a YAML name that differs from
        # the discovered hostname; ``_on_import_update`` already
        # filters configured + ignored entries so re-emitting
        # the full set is cheap.
        controller._state_monitor.revisit_all_importables()
        # Drop the monitor's per-name state. Both the reachability
        # history and the source-precedence ledger would otherwise
        # accumulate one entry per device that's ever lived in the
        # catalog (the mDNS Removed branch only fires on broadcast
        # disappearance, not YAML deletion); a stale state_source
        # also gates a name reused by a later re-add.
        controller._reachability.clear(device.name)
        controller._state_monitor.forget(device.name)
        # Idempotent for the controller-driven delete/archive
        # paths; the safety net is external ``rm`` / rename.
        controller._metadata_store.clear_volatile(device.configuration)
