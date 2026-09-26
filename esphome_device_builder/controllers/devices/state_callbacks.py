"""Per-attribute mDNS state callbacks for ``DevicesController``."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ...helpers.device_yaml import pending_changes_via_hash
from ...helpers.mac_addresses import derive_interface_macs
from ...models import (
    Device,
    DeviceState,
    DeviceStateChangedData,
    EventType,
    ReachabilitySource,
)

if TYPE_CHECKING:
    from .controller import DevicesController

_LOGGER = logging.getLogger(__name__)


def _apply_logged_observation(
    controller: DevicesController,
    name: str,
    field_name: str,
    value: Any,
    log_label: str,
    on_change: Callable[[Device], None] | None = None,
) -> None:
    """Apply *value* to ``device.runtime_state.<field_name>``; log, persist, fire DEVICE_UPDATED."""
    for device in controller._devices_by_name(name):
        old = getattr(device.runtime_state, field_name)
        if old == value:
            continue
        setattr(device.runtime_state, field_name, value)
        if on_change is not None:
            on_change(device)
        log = _LOGGER.info if old else _LOGGER.debug
        log(
            "Device %s (%s) %s: %s → %s (via mdns)",
            name,
            device.configuration,
            log_label,
            old or "?",
            value,
        )
        controller._metadata_store.update(device.configuration, **{field_name: value})
        controller._fire_device_updated(device)


def on_state_change(
    controller: DevicesController, name: str, state: DeviceState, source: str
) -> None:
    """Forward state monitor updates onto the event bus."""
    for device in controller._devices_by_name(name):
        old_state = device.runtime_state.state
        device.runtime_state.state = state
        _stamp_offline_since(controller, device, old_state, state)
        _LOGGER.info(
            "Device %s (%s): %s → %s (via %s)",
            name,
            device.configuration,
            old_state,
            state,
            source,
        )
        # Match ``DeviceStateChangedEventData``'s flat
        # ``{configuration, state}`` shape; firing the full
        # ``device`` object made the frontend's destructure resolve
        # both fields to ``undefined``.
        controller._db.bus.fire(
            EventType.DEVICE_STATE_CHANGED,
            DeviceStateChangedData(
                configuration=device.configuration,
                state=state.value,
            ),
        )


def on_source_change(controller: DevicesController, name: str, source: ReachabilitySource) -> None:
    """Update ``active_source`` and fire DEVICE_UPDATED; also clears ``deployed_name``."""
    devices = controller._devices_by_name(name)
    # Only an announce that maps to one config proves the firmware carries the name;
    # above the dedupe since a same-path rename carries ``active_source`` forward.
    if source is ReachabilitySource.MDNS and len(devices) == 1:
        controller._clear_deployed_name(devices[0].configuration, device=devices[0])
    for device in devices:
        if device.runtime_state.active_source == source:
            continue
        device.runtime_state.active_source = source
        controller._fire_device_updated(device)


def on_deployed_identity_live_change(
    controller: DevicesController, name: str, *, live: bool
) -> None:
    """Update ``deployed_identity_live`` and fire DEVICE_UPDATED; runtime-only, not persisted."""
    for device in controller._devices_by_name(name):
        if device.runtime_state.deployed_identity_live == live:
            continue
        device.runtime_state.deployed_identity_live = live
        controller._fire_device_updated(device)


def on_ip_change(controller: DevicesController, name: str, ip: str, addresses: list[str]) -> None:
    """Forward IP updates onto the event bus and persist the primary value."""
    new_addresses = list(addresses)
    for device in controller._devices_by_name(name):
        if device.ip == ip and device.runtime_state.ip_addresses == new_addresses:
            continue
        if device.ip != ip:
            device.ip = ip
            controller._metadata_store.update(device.configuration, ip=ip)
        device.runtime_state.ip_addresses = list(new_addresses)
        _LOGGER.debug(
            "Device %s (%s) IPs: %s",
            name,
            device.configuration,
            ", ".join(new_addresses),
        )
        controller._fire_device_updated(device)


def on_resolved_addresses_cleared(controller: DevicesController, name: str) -> None:
    """
    Clear the resolved set after a confirmed loss of mDNS resolution.

    ``device.ip`` keeps the last-known primary in RAM and on disk so
    the OTA address cache and the api_reviver's cohort gate survive
    offline windows.
    """
    for device in controller._devices_by_name(name):
        if not device.runtime_state.ip_addresses:
            continue
        device.runtime_state.ip_addresses = []
        _LOGGER.debug("Device %s (%s) IPs: (cleared)", name, device.configuration)
        controller._fire_device_updated(device)


def on_persisted_ip_invalidated(controller: DevicesController, name: str, stale_ip: str) -> None:
    """
    Clear a persisted last-known IP the reviver proved belongs to another device.

    The explicit counterpart to :func:`on_ip_change`'s keep-on-disk
    contract — only identity-verified evidence gets to drop the value,
    and only from devices still holding the proven-stale IP (a
    same-name sibling's independent IP, or one mDNS re-learned while
    the dial was in flight, is not what the mismatch disproved).
    """
    for device in controller._devices_by_name(name):
        if device.ip != stale_ip:
            continue
        _LOGGER.info(
            "Device %s (%s): clearing stale persisted IP %s",
            name,
            device.configuration,
            device.ip,
        )
        device.ip = ""
        controller._metadata_store.update(device.configuration, ip="")
        controller._fire_device_updated(device)


def on_version_change(controller: DevicesController, name: str, version: str) -> None:
    """Apply a fresh ESPHome version observed via mDNS."""

    def _flip_update_available(device: Device) -> None:
        device.update_available = bool(device.current_version and version != device.current_version)

    _apply_logged_observation(
        controller,
        name,
        "deployed_version",
        version,
        log_label="version",
        on_change=_flip_update_available,
    )


def on_mac_address_change(controller: DevicesController, name: str, mac: str) -> None:
    """Apply a MAC address observed via mDNS and derive interface MACs."""
    for device in controller._devices_by_name(name):
        if device.mac_address == mac:
            continue
        device.mac_address = mac
        device.ethernet_mac, device.bluetooth_mac = derive_interface_macs(
            mac, device.target_platform, device.loaded_integrations
        )
        controller._db.create_background_task(
            controller._persist_device_metadata_async(device.configuration, mac_address=mac)
        )
        controller._fire_device_updated(device)


def on_api_encryption_change(controller: DevicesController, name: str, encryption: str) -> None:
    """Apply the API-encryption state observed via mDNS.

    A truthy wire cipher promotes ``api_encrypted`` to True;
    the scan-time YAML check misses Jinja-templated ``packages``
    (issue #437).
    """
    for device in controller._devices_by_name(name):
        wire_promotes_encrypted = bool(encryption) and not device.api_encrypted
        if device.runtime_state.api_encryption_active == encryption and not wire_promotes_encrypted:
            continue
        device.runtime_state.api_encryption_active = encryption
        if wire_promotes_encrypted:
            device.api_encrypted = True
        # ``set_field`` (not ``update``): empty string is the
        # plaintext-confirmed marker, distinct from ``None``.
        controller._metadata_store.set_field(
            device.configuration, "api_encryption_active", encryption
        )
        controller._fire_device_updated(device)


def on_config_hash_change(controller: DevicesController, name: str, config_hash: str) -> None:
    """Apply a running-firmware config hash observed via mDNS."""

    def _flip_pending(device: Device) -> None:
        if device.expected_config_hash:
            device.has_pending_changes = device.expected_config_hash != config_hash
            device.pending_changes_via_hash = pending_changes_via_hash(
                device.expected_config_hash, config_hash
            )

    _apply_logged_observation(
        controller,
        name,
        "deployed_config_hash",
        config_hash,
        log_label="config_hash",
        on_change=_flip_pending,
    )


def _stamp_offline_since(
    controller: DevicesController,
    device: Device,
    old_state: DeviceState,
    state: DeviceState,
) -> None:
    """Queue the ``offline_since`` change for *device*; ``devices/list`` persists it."""
    configuration = device.configuration
    stored = controller._metadata_store.get_field(configuration, "offline_since")
    if state is DeviceState.ONLINE:
        if stored or controller.state.pending_offline_since.get(configuration):
            controller.state.pending_offline_since[configuration] = None
        return
    if old_state is DeviceState.ONLINE:
        controller.state.pending_offline_since[configuration] = time.time()
        return
    # Startup settle: the device was already unreachable, so a stamp from a
    # previous run is the only honest anchor. Seed one only when there is
    # none, or every dashboard restart would reset the clock to zero.
    if not stored and configuration not in controller.state.pending_offline_since:
        controller.state.pending_offline_since[configuration] = time.time()
