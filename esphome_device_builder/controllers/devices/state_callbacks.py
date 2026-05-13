"""Per-attribute mDNS state callbacks for ``DevicesController``."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ...helpers.mac_addresses import derive_interface_macs
from ...models import DeviceState, DeviceStateChangedData, EventType

if TYPE_CHECKING:
    from .controller import DevicesController

_LOGGER = logging.getLogger(__name__)


def on_state_change(
    controller: DevicesController, name: str, state: DeviceState, source: str
) -> None:
    """Forward state monitor updates onto the event bus."""
    for device in controller._devices_by_name(name):
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


def on_ip_change(controller: DevicesController, name: str, ip: str, addresses: list[str]) -> None:
    """
    Forward IP updates onto the event bus and persist the primary value.

    ``ip=""`` (empty *addresses*) means the device dropped off
    mDNS; the last-known primary stays on disk so the OTA
    address cache survives the offline window.
    """
    new_addresses = list(addresses)
    for device in controller._devices_by_name(name):
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
            controller._db.create_background_task(
                controller._persist_device_ip_async(device.configuration, ip)
            )
        controller._fire_device_updated(device)


def on_version_change(controller: DevicesController, name: str, version: str) -> None:
    """Apply a fresh ESPHome version observed via mDNS."""
    for device in controller._devices_by_name(name):
        if device.deployed_version == version:
            continue

        # StorageJSON.load/save are blocking; push to a background
        # task so the loop's exception handler surfaces failures.
        controller._db.create_background_task(
            controller._persist_storage_version_async(device.configuration, version)
        )

        old_version = device.deployed_version
        device.deployed_version = version
        device.update_available = bool(device.current_version and version != device.current_version)
        _LOGGER.info(
            "Device %s (%s) version: %s → %s (via mdns)",
            name,
            device.configuration,
            old_version or "?",
            version,
        )
        controller._fire_device_updated(device)


def on_mac_address_change(controller: DevicesController, name: str, mac: str) -> None:
    """
    Apply a MAC address observed via mDNS and derive interface MACs.

    Only the primary MAC is persisted; ``ethernet_mac`` /
    ``bluetooth_mac`` are recomputed via
    :func:`derive_interface_macs` from primary +
    ``loaded_integrations`` on each apply.
    """
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
    """
    Apply the API-encryption state observed via mDNS.

    Promotes ``api_encrypted`` to True on a truthy cipher; the
    scan-time YAML pass misses Jinja-templated ``packages`` so
    the wire signal is the truthful one (issue #437). The
    empty-string broadcast deliberately doesn't clear the flag,
    leaving the wire-says-no / YAML-says-yes "mismatch" /
    "pending" shape to the existing state machine.
    """
    for device in controller._devices_by_name(name):
        wire_promotes_encrypted = bool(encryption) and not device.api_encrypted
        if device.api_encryption_active == encryption and not wire_promotes_encrypted:
            continue
        device.api_encryption_active = encryption
        if wire_promotes_encrypted:
            device.api_encrypted = True
        controller._fire_device_updated(device)


def on_config_hash_change(controller: DevicesController, name: str, config_hash: str) -> None:
    """
    Apply a running-firmware config hash observed via mDNS.

    Flips ``has_pending_changes`` against the expected hash when
    both are known; firmware predating the ``config_hash`` TXT
    broadcast never triggers this callback and stays on the
    legacy mtime check.
    """
    for device in controller._devices_by_name(name):
        if device.deployed_config_hash == config_hash:
            continue
        old_hash = device.deployed_config_hash
        device.deployed_config_hash = config_hash
        # Mtime side stays with the periodic scanner poll so this
        # callback can stay off-disk; a YAML edit between polls
        # (~5s) self-corrects on the next scan.
        if device.expected_config_hash:
            device.has_pending_changes = device.expected_config_hash != config_hash
        _LOGGER.info(
            "Device %s (%s) config_hash: %s → %s (via mdns)",
            name,
            device.configuration,
            old_hash or "?",
            config_hash,
        )
        controller._fire_device_updated(device)
