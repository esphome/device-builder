"""Tests for the importable-device discovery plumbing.

Covers the bridge between upstream esphome's ``DashboardImportDiscovery``
and our dashboard event bus / ``import_result`` cache. The browser
itself is upstream code — what we own is the translation from
``DiscoveredImport`` to ``AdoptableDevice``, the configured-device
filter, and the ignore flag.
"""

from __future__ import annotations

from esphome.zeroconf import DashboardImportDiscovery, DiscoveredImport
from zeroconf.asyncio import AsyncServiceInfo

from esphome_device_builder.models import AdoptableDevice, Device, DeviceState

from .conftest import make_state_monitor_with_callbacks


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


def _added(callbacks) -> list[AdoptableDevice]:
    """Pull every ``AdoptableDevice`` argument the recorder saw on importable_added."""
    return [call[1] for call in callbacks.calls if call[0] == "on_importable_added"]


def _removed(callbacks) -> list[str]:
    """Pull every name argument the recorder saw on importable_removed."""
    return [call[1] for call in callbacks.calls if call[0] == "on_importable_removed"]


def test_on_import_update_translates_to_adoptable_device() -> None:
    monitor, callbacks = make_state_monitor_with_callbacks([])

    monitor._on_import_update("kitchen-1a2b3c._esphomelib._tcp.local.", _discovered())

    assert _added(callbacks) == [
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
    monitor, callbacks = make_state_monitor_with_callbacks([])

    monitor._on_import_update("kitchen-1a2b3c._esphomelib._tcp.local.", None)

    # The mDNS service name is sliced down to the device-name label so
    # the dashboard can index ``import_result`` by ``device.name``.
    assert _removed(callbacks) == ["kitchen-1a2b3c"]


def test_on_import_update_skips_already_configured_devices() -> None:
    """Configured devices never surface as importable."""
    monitor, callbacks = make_state_monitor_with_callbacks([_device("kitchen-1a2b3c")])

    monitor._on_import_update("kitchen-1a2b3c._esphomelib._tcp.local.", _discovered())
    assert _added(callbacks) == []


def test_on_import_update_threads_ignored_flag() -> None:
    """The ignored set drives the ``ignored`` flag on the AdoptableDevice."""
    ignored = {"kitchen-1a2b3c"}
    monitor, callbacks = make_state_monitor_with_callbacks([])
    monitor._is_ignored = ignored.__contains__

    monitor._on_import_update("kitchen-1a2b3c._esphomelib._tcp.local.", _discovered())
    added = _added(callbacks)
    assert len(added) == 1 and added[0].ignored is True


def test_on_import_update_friendly_name_none_becomes_empty_string() -> None:
    """``DiscoveredImport.friendly_name`` is Optional; AdoptableDevice expects str."""
    monitor, callbacks = make_state_monitor_with_callbacks([])

    discovered = DiscoveredImport(
        friendly_name=None,
        device_name="kitchen",
        package_import_url="github://x",
        project_name="x",
        project_version="1.0",
        network="wifi",
    )
    monitor._on_import_update("kitchen._esphomelib._tcp.local.", discovered)

    assert _added(callbacks)[0].friendly_name == ""


def test_get_importable_devices_filters_configured() -> None:
    """``get_importable_devices`` rebuilds the snapshot, dropping configured."""
    monitor, _callbacks = make_state_monitor_with_callbacks([_device("garage")])
    # Stand in for a started DashboardImportDiscovery — populate its
    # ``import_state`` directly so we don't have to spin up zeroconf.
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
    monitor, _callbacks = make_state_monitor_with_callbacks([])
    assert monitor.get_importable_devices() == []


def test_revisit_importable_refires_added_when_cached() -> None:
    """``revisit_importable`` re-emits the callback for cached entries.

    Upstream's ``DashboardImportDiscovery`` only calls ``on_update``
    on first sight (``is_new`` check). When a configured device that
    was hiding a discovered entry gets removed, no fresh announcement
    fires; we have to nudge the cache ourselves.
    """
    monitor, callbacks = make_state_monitor_with_callbacks([])  # device just got deleted
    monitor._import_discovery = DashboardImportDiscovery()
    monitor._import_discovery.import_state = {
        "kitchen-1a2b3c._esphomelib._tcp.local.": _discovered("kitchen-1a2b3c"),
    }

    monitor.revisit_importable("kitchen-1a2b3c")

    added = _added(callbacks)
    assert len(added) == 1
    assert added[0].name == "kitchen-1a2b3c"


def test_revisit_importable_noop_for_unknown_name() -> None:
    """No cached entry → no callback fires (and no crash)."""
    monitor, callbacks = make_state_monitor_with_callbacks([])
    monitor._import_discovery = DashboardImportDiscovery()
    monitor._import_discovery.import_state = {}

    monitor.revisit_importable("unknown")

    assert _added(callbacks) == []


def test_revisit_importable_noop_when_browser_not_started() -> None:
    """No browser → silent skip (no crash on the optional attr)."""
    monitor, _callbacks = make_state_monitor_with_callbacks([])
    monitor.revisit_importable("kitchen")  # must not raise


def test_apply_http_service_info_populates_web_url_and_refires() -> None:
    """An HTTP service announcement decorates the cached importable.

    Sequence: ``_esphomelib._tcp.local.`` arrives first → AdoptableDevice
    surfaces with ``web_url=""``. Then ``_http._tcp.local.`` arrives;
    we store the URL and re-fire ADDED so the frontend updates the
    card's Visit-web-UI link in place.
    """
    monitor, callbacks = make_state_monitor_with_callbacks([])
    monitor._import_discovery = DashboardImportDiscovery()
    monitor._import_discovery.import_state = {
        "kitchen._esphomelib._tcp.local.": _discovered("kitchen"),
    }

    info = AsyncServiceInfo("_http._tcp.local.", "kitchen._http._tcp.local.")
    info.server = "kitchen.local."
    info.port = 80

    monitor._apply_http_service_info("kitchen", info)

    assert monitor.state.http_urls == {"kitchen": "http://kitchen.local"}
    added = _added(callbacks)
    assert len(added) == 1
    assert added[0].web_url == "http://kitchen.local"


def test_apply_http_service_info_includes_non_default_port() -> None:
    """Non-port-80 services build URLs with the explicit ``:port`` suffix."""
    monitor, callbacks = make_state_monitor_with_callbacks([])
    monitor._import_discovery = DashboardImportDiscovery()
    monitor._import_discovery.import_state = {
        "kitchen._esphomelib._tcp.local.": _discovered("kitchen"),
    }

    info = AsyncServiceInfo("_http._tcp.local.", "kitchen._http._tcp.local.")
    info.server = "kitchen.local."
    info.port = 8080

    monitor._apply_http_service_info("kitchen", info)
    assert _added(callbacks)[0].web_url == "http://kitchen.local:8080"


def test_apply_http_service_info_skips_when_unchanged() -> None:
    """Repeat announcements for the same URL don't re-fire ADDED."""
    monitor, callbacks = make_state_monitor_with_callbacks([])
    monitor._import_discovery = DashboardImportDiscovery()
    monitor._import_discovery.import_state = {
        "kitchen._esphomelib._tcp.local.": _discovered("kitchen"),
    }

    info = AsyncServiceInfo("_http._tcp.local.", "kitchen._http._tcp.local.")
    info.server = "kitchen.local."
    info.port = 80

    monitor._apply_http_service_info("kitchen", info)
    monitor._apply_http_service_info("kitchen", info)

    # Single fire — duplicate calls are deduped by URL equality.
    assert len(_added(callbacks)) == 1


def test_revisit_importable_skips_ignored_devices() -> None:
    """Ignored devices stay hidden after deletion.

    The user already said "don't show me this"; a YAML deletion
    shouldn't unilaterally bring it back into the banner. They can
    unignore through the menu if they change their mind.
    """
    ignored = {"kitchen-1a2b3c"}
    monitor, callbacks = make_state_monitor_with_callbacks([])
    monitor._is_ignored = ignored.__contains__
    monitor._import_discovery = DashboardImportDiscovery()
    monitor._import_discovery.import_state = {
        "kitchen-1a2b3c._esphomelib._tcp.local.": _discovered("kitchen-1a2b3c"),
    }

    monitor.revisit_importable("kitchen-1a2b3c")

    assert _added(callbacks) == []
