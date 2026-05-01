"""Tests for the importable-device discovery plumbing.

Covers the bridge between upstream esphome's ``DashboardImportDiscovery``
and our dashboard event bus / ``import_result`` cache. The browser
itself is upstream code — what we own is the translation from
``DiscoveredImport`` to ``AdoptableDevice``, the configured-device
filter, and the ignore flag.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from esphome.zeroconf import DiscoveredImport

from esphome_device_builder.controllers._device_state_monitor import DeviceStateMonitor
from esphome_device_builder.models import AdoptableDevice, Device, DeviceState


def _device(name: str) -> Device:
    return Device(
        name=name,
        friendly_name=name,
        configuration=f"{name}.yaml",
        address=f"{name}.local",
        state=DeviceState.UNKNOWN,
    )


def _discovered(device_name: str = "kitchen-1a2b3c") -> DiscoveredImport:
    return DiscoveredImport(
        friendly_name="Kitchen",
        device_name=device_name,
        package_import_url="github://acme/firmware/kitchen.yaml@main",
        project_name="acme.kitchen",
        project_version="2026.05.01",
        network="wifi",
    )


def test_on_import_update_translates_to_adoptable_device() -> None:
    added: list[AdoptableDevice] = []
    monitor = DeviceStateMonitor(
        get_devices=lambda: [],
        on_state_change=MagicMock(),
        on_ip_change=MagicMock(),
        on_importable_added=added.append,
    )

    monitor._on_import_update("kitchen-1a2b3c._esphomelib._tcp.local.", _discovered())

    assert added == [
        AdoptableDevice(
            name="kitchen-1a2b3c",
            friendly_name="Kitchen",
            package_import_url="github://acme/firmware/kitchen.yaml@main",
            project_name="acme.kitchen",
            project_version="2026.05.01",
            network="wifi",
            ignored=False,
        )
    ]


def test_on_import_update_emits_removed_with_device_name() -> None:
    removed: list[str] = []
    monitor = DeviceStateMonitor(
        get_devices=lambda: [],
        on_state_change=MagicMock(),
        on_ip_change=MagicMock(),
        on_importable_removed=removed.append,
    )

    monitor._on_import_update("kitchen-1a2b3c._esphomelib._tcp.local.", None)

    # The mDNS service name is sliced down to the device-name label so
    # the dashboard can index ``import_result`` by ``device.name``.
    assert removed == ["kitchen-1a2b3c"]


def test_on_import_update_skips_already_configured_devices() -> None:
    """Configured devices never surface as importable."""
    added: list[AdoptableDevice] = []
    monitor = DeviceStateMonitor(
        get_devices=lambda: [_device("kitchen-1a2b3c")],
        on_state_change=MagicMock(),
        on_ip_change=MagicMock(),
        on_importable_added=added.append,
    )

    monitor._on_import_update("kitchen-1a2b3c._esphomelib._tcp.local.", _discovered())
    assert added == []


def test_on_import_update_threads_ignored_flag() -> None:
    """The ignored set drives the ``ignored`` flag on the AdoptableDevice."""
    added: list[AdoptableDevice] = []
    ignored = {"kitchen-1a2b3c"}
    monitor = DeviceStateMonitor(
        get_devices=lambda: [],
        on_state_change=MagicMock(),
        on_ip_change=MagicMock(),
        on_importable_added=added.append,
        is_ignored=ignored.__contains__,
    )

    monitor._on_import_update("kitchen-1a2b3c._esphomelib._tcp.local.", _discovered())
    assert len(added) == 1 and added[0].ignored is True


def test_on_import_update_friendly_name_none_becomes_empty_string() -> None:
    """``DiscoveredImport.friendly_name`` is Optional; AdoptableDevice expects str."""
    added: list[AdoptableDevice] = []
    monitor = DeviceStateMonitor(
        get_devices=lambda: [],
        on_state_change=MagicMock(),
        on_ip_change=MagicMock(),
        on_importable_added=added.append,
    )

    discovered = DiscoveredImport(
        friendly_name=None,
        device_name="kitchen",
        package_import_url="github://x",
        project_name="x",
        project_version="1.0",
        network="wifi",
    )
    monitor._on_import_update("kitchen._esphomelib._tcp.local.", discovered)

    assert added[0].friendly_name == ""


def test_get_importable_devices_filters_configured() -> None:
    """``get_importable_devices`` rebuilds the snapshot, dropping configured."""
    monitor = DeviceStateMonitor(
        get_devices=lambda: [_device("garage")],
        on_state_change=MagicMock(),
        on_ip_change=MagicMock(),
        is_ignored=lambda _: False,
    )
    # Stand in for a started DashboardImportDiscovery — populate its
    # ``import_state`` directly so we don't have to spin up zeroconf.
    from esphome.zeroconf import DashboardImportDiscovery

    monitor._import_discovery = DashboardImportDiscovery()
    monitor._import_discovery.import_state = {
        "kitchen._esphomelib._tcp.local.": _discovered("kitchen"),
        "garage._esphomelib._tcp.local.": _discovered("garage"),
    }

    snapshot = monitor.get_importable_devices()

    names = sorted(d.name for d in snapshot)
    assert names == ["kitchen"]


def test_get_importable_devices_returns_empty_before_browser_start() -> None:
    """Without a started browser the snapshot is just empty (no crash)."""
    monitor = DeviceStateMonitor(
        get_devices=lambda: [],
        on_state_change=MagicMock(),
        on_ip_change=MagicMock(),
    )
    assert monitor.get_importable_devices() == []
