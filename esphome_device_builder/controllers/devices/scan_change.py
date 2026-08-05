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
        # The mDNS half short-circuits to the zeroconf cache when
        # present; the paired ICMP wake covers ping-only devices that
        # don't broadcast ``_esphomelib._tcp``. Without the nudge,
        # YAMLs dropped on disk outside the API entrypoints (git
        # clone, copy from another dashboard) sit at "Unknown" until
        # the next periodic sweep; a cold-start herd of wakes is
        # absorbed into the first post-bootstrap sweep.
        controller._state_monitor.probe_reachability(device.name)
        # Drop the stale importable row so connected subscribe_events
        # clients stop showing the adopt banner once the device is
        # configured. Idempotent: fires REMOVED only if a row existed.
        controller._on_importable_removed(device.name)
    _reconcile_importables_on_rename(controller, kind, device, previous)
    if (
        kind in (ScanChange.UPDATED, ScanChange.RELOADED)
        and previous is not None
        and previous.address != device.address
    ):
        # The change swapped in a new address (a ``wifi.use_address``
        # edit, or the post-regen StorageJSON replacing the
        # ``<file>.local`` fallback); without the retarget the sweep
        # keeps pinging addresses resolved from the old one (#2486) or
        # waits out the remainder of the periodic interval.
        controller._state_monitor.address_retargeted(device.name)
    if kind in (ScanChange.UPDATED, ScanChange.RELOADED, ScanChange.REMOVED):
        # YAML cache key changed (or a reload re-read it); clear any
        # prior failure marker so the next edit gets a fresh chance at
        # ``--only-generate`` (and re-creating a deleted file
        # later doesn't inherit the old failure).
        controller.state.regenerate_failed.discard(device.configuration)
    if kind in (ScanChange.ADDED, ScanChange.UPDATED, ScanChange.RELOADED):
        _sync_network_fingerprint(controller, kind, device)
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
        controller._state_monitor.importable.revisit_all_importables()
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


def _reconcile_importables_on_rename(
    controller: DevicesController,
    kind: ScanChange,
    device: Device,
    previous: Device | None,
) -> None:
    """Retract the corrected name's importable row and resurface the freed one."""
    if (
        kind not in (ScanChange.UPDATED, ScanChange.RELOADED)
        or previous is None
        or previous.name == device.name
    ):
        return
    # The ADDED retraction keyed on the name at add time; when a later
    # reload corrects it (a rename, or a package-provided name the
    # first load couldn't resolve), the importable row for the
    # corrected name would otherwise stay visible to connected clients
    # until the next ADDED/REMOVED. The freed old name is the other
    # half of the transition: a cached announcement the configured-name
    # filter was suppressing can resurface now.
    controller._on_importable_removed(device.name)
    controller._state_monitor.importable.revisit_importable(previous.name)


def _sync_network_fingerprint(
    controller: DevicesController, kind: ScanChange, device: Device
) -> None:
    """Seed the stored fingerprint; regen when an out-of-band edit moved it."""
    if not device.network_fingerprint:
        # Empty means the YAML was unreadable this sweep (or carries no
        # address-source block at all): no information, so neither
        # compare nor overwrite the stored digest.
        return
    stored = controller._metadata_store.get(device.configuration).get("network_fingerprint")
    if stored == device.network_fingerprint:
        return
    # An out-of-band edit (git pull, external editor — live as UPDATED,
    # or across a restart as ADDED) moved an address-source block;
    # without a regen ``StorageJSON.address`` keeps the old
    # ``use_address`` and the sweep pings a stale host (#2486).
    # Deliberately over-broad — ``substitutions:`` / ``esphome:`` edits
    # that touch no address cost one deduped regen. RELOADED is our own
    # doing (API-path writes regen in ``_persist_yaml_mutation``) and
    # only re-seeds; ``stored is None`` is first sight, nothing to
    # compare.
    if stored is not None and kind is not ScanChange.RELOADED:
        controller._schedule_storage_regenerate(device.configuration)
    controller._metadata_store.set_field(
        device.configuration, "network_fingerprint", device.network_fingerprint
    )
