"""Per-attribute mDNS state callbacks for ``DevicesController``."""

from __future__ import annotations

import logging
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
    """Update ``active_source`` and fire DEVICE_UPDATED; runtime-only, not persisted."""
    for device in controller._devices_by_name(name):
        if device.runtime_state.active_source == source:
            continue
        device.runtime_state.active_source = source
        controller._fire_device_updated(device)


def on_ip_change(controller: DevicesController, name: str, ip: str, addresses: list[str]) -> None:
    """Forward IP updates onto the event bus and persist the primary value.

    ``ip=""`` (empty *addresses*) keeps the last-known primary
    on disk so the OTA address cache survives offline windows.
    """
    new_addresses = list(addresses)
    for device in controller._devices_by_name(name):
        if device.ip == ip and device.runtime_state.ip_addresses == new_addresses:
            continue
        ip_changed = device.ip != ip
        device.ip = ip
        device.runtime_state.ip_addresses = list(new_addresses)
        _LOGGER.debug(
            "Device %s (%s) IPs: %s",
            name,
            device.configuration,
            ", ".join(new_addresses) or "(cleared)",
        )
        if ip and ip_changed:
            controller._metadata_store.update(device.configuration, ip=ip)
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
