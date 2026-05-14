"""Regression tests for the mDNS service-name → device lookup.

mDNS broadcasts ``<device-name>._esphomelib._tcp.local.``; the
left-hand label is the device's ``esphome.name`` verbatim. Modern
configs use ``friendly_name_slugify``-style names with hyphens
(``apollo-r-pro-1-eth-5938e0``); the previous code converted those
hyphens to underscores before lookup, so every modern device's
mDNS announcement landed on a non-existent ``apollo_r_pro_...`` key
and the device stayed marked Unknown forever.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from esphome import zeroconf as esphome_zc
from zeroconf import ServiceStateChange

from esphome_device_builder.controllers._device_state_monitor import (
    DeviceStateMonitor,
    device_name_from_service,
)
from esphome_device_builder.controllers._device_state_monitor import controller as monitor_module
from esphome_device_builder.models import Device, DeviceState

from .conftest import make_state_monitor_with_callbacks


def _device(name: str) -> Device:
    return Device(
        name=name,
        friendly_name=name,
        configuration=f"{name}.yaml",
        address=f"{name}.local",
        state=DeviceState.UNKNOWN,
    )


# ----------------------------------------------------------------------
# device_name_from_service helper — the bit ``_on_service_state_change``
# actually uses to compute the catalog key.
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "service_name,expected",
    [
        # Modern hyphenated device — the previously-failing case.
        ("apollo-r-pro-1-eth-5938e0._esphomelib._tcp.local.", "apollo-r-pro-1-eth-5938e0"),
        ("home-assistant-voice-090073._esphomelib._tcp.local.", "home-assistant-voice-090073"),
        # Underscored YAML name (older convention) — must still work.
        ("legacy_device._esphomelib._tcp.local.", "legacy_device"),
        # Single-word name — sanity check.
        ("steamreset._esphomelib._tcp.local.", "steamreset"),
    ],
)
def test_device_name_from_service_preserves_label(service_name: str, expected: str) -> None:
    """The label is returned verbatim — no hyphen↔underscore substitution."""
    assert device_name_from_service(service_name) == expected


# ----------------------------------------------------------------------
# _on_service_state_change end-to-end (stubbed browser)
# ----------------------------------------------------------------------


class _FakeServiceInfo:
    """Stand-in for ``AsyncServiceInfo`` whose cache always hits.

    Lets us drive ``_on_service_state_change``'s synchronous path
    without booting real zeroconf — the handler calls
    ``info.load_from_cache(zeroconf)`` first and only spawns a
    network-resolve task on miss.
    """

    def __init__(self, _service_type: str, _name: str) -> None:
        # ``DashboardImportDiscovery.browser_callback`` (also driven
        # by the dispatch handler) reads ``info.properties`` looking
        # for ``package_import_url`` TXT records. Empty dict means
        # "not an importable device" so it bails out cleanly without
        # touching real zeroconf state.
        self.properties: dict[bytes, bytes] = {}

    def load_from_cache(self, _zc: Any) -> bool:
        return True

    def parsed_scoped_addresses(self, _ip_version: Any) -> list[str]:
        return []

    @property
    def decoded_properties(self) -> dict[str, str | None]:
        return {}


async def _capture_handler(monitor: DeviceStateMonitor, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Boot the mDNS browser with stubs, return the inner handler."""
    captured: dict[str, Any] = {}

    class _FakeBrowser:
        def __init__(self, _zc: Any, _service_types: Any, *, handlers: list[Any]) -> None:
            captured["handler"] = handlers[0]

    fake_zc = MagicMock()
    monkeypatch.setattr(monitor_module, "AsyncEsphomeZeroconf", lambda: fake_zc)
    monkeypatch.setattr(monitor_module, "AsyncServiceInfo", _FakeServiceInfo)
    monkeypatch.setattr(monitor_module, "AsyncServiceBrowser", _FakeBrowser)
    # Upstream ``DashboardImportDiscovery.browser_callback`` builds
    # its own ``AsyncServiceInfo`` from the ``esphome.zeroconf``
    # module — patch that copy too so the dispatch handler can fan
    # the same event through the upstream callback without touching
    # real zeroconf.
    monkeypatch.setattr(esphome_zc, "AsyncServiceInfo", _FakeServiceInfo)

    await monitor._start_mdns_browser()
    return captured["handler"]


async def test_handler_marks_hyphenated_device_online(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hyphenated mDNS announcement marks the matching catalog entry online.

    Pre-fix the handler did ``.replace("-", "_")`` before lookup, so
    ``apollo-r-pro-1-eth-5938e0`` matched nothing in the catalog and
    the device stayed Unknown until the 60s ping sweep.
    """
    devices = [_device("apollo-r-pro-1-eth-5938e0")]
    monitor, callbacks = make_state_monitor_with_callbacks(devices)

    handler = await _capture_handler(monitor, monkeypatch)

    handler(
        MagicMock(),
        "_esphomelib._tcp.local.",
        "apollo-r-pro-1-eth-5938e0._esphomelib._tcp.local.",
        ServiceStateChange.Added,
    )

    assert (
        "on_state_change",
        "apollo-r-pro-1-eth-5938e0",
        DeviceState.ONLINE,
        "mdns",
    ) in callbacks.calls


async def test_handler_does_not_substitute_hyphens(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hyphenated YAML must not be looked up via underscores.

    Catches a regression that re-introduces the hyphen substitution:
    a device named ``my-device`` would then never see its mDNS
    announcement reach the catalog if the handler turned the label
    into ``my_device`` before lookup.
    """
    devices = [_device("my-device")]
    # The recorder's per-device state-flip mirrors production —
    # without it, ``device.state`` stays UNKNOWN forever and the
    # eager ``apply(ONLINE)`` in the browser callback plus the
    # redundant claim in ``_apply_service_info`` would each re-fire
    # against the still-stale state, yielding a misleading double-call.
    monitor, callbacks = make_state_monitor_with_callbacks(devices)

    handler = await _capture_handler(monitor, monkeypatch)
    handler(
        MagicMock(),
        "_esphomelib._tcp.local.",
        "my-device._esphomelib._tcp.local.",
        ServiceStateChange.Added,
    )

    state_calls = [c for c in callbacks.calls if c[0] == "on_state_change"]
    assert state_calls == [("on_state_change", "my-device", DeviceState.ONLINE, "mdns")]


async def test_handler_short_circuits_unknown_device(monkeypatch: pytest.MonkeyPatch) -> None:
    """An mDNS announcement for an unconfigured device is ignored cheaply.

    Otherwise we'd construct an ``AsyncServiceInfo`` and hit the
    cache for every unrelated ESPHome device on the LAN — wasted
    work that scales with the size of the user's network.
    """
    monitor, callbacks = make_state_monitor_with_callbacks([])  # empty catalog

    handler = await _capture_handler(monitor, monkeypatch)
    handler(
        MagicMock(),
        "_esphomelib._tcp.local.",
        "stranger-on-lan._esphomelib._tcp.local.",
        ServiceStateChange.Added,
    )

    assert not any(c[0] == "on_state_change" for c in callbacks.calls)


async def test_mdns_takes_ownership_after_ping_set_online(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An mDNS announcement claims ownership even if ping already set ONLINE.

    Pre-fix, ``apply()``'s "no-op when state is unchanged" early return
    meant a device that ping had already flipped to ONLINE would stay
    owned by ``ping`` — letting a future ping-OFFLINE observation
    override the still-true mDNS view.
    """
    devices = [_device("kitchen")]
    devices[0].state = DeviceState.ONLINE  # ping already saw it
    monitor, _callbacks = make_state_monitor_with_callbacks(devices)
    monitor.state.state_source["kitchen"] = "ping"

    handler = await _capture_handler(monitor, monkeypatch)
    handler(
        MagicMock(),
        "_esphomelib._tcp.local.",
        "kitchen._esphomelib._tcp.local.",
        ServiceStateChange.Added,
    )

    assert monitor.priority_for("kitchen") == "mdns"
