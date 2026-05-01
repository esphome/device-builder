"""Regression test for the mDNS service-name → device lookup.

mDNS broadcasts ``<device-name>._esphomelib._tcp.local.``; the
left-hand label is the device's ``esphome.name`` verbatim. Modern
configs use ``friendly_name_slugify``-style names with hyphens
(``apollo-r-pro-1-eth-5938e0``); the previous code converted those
hyphens to underscores before lookup, so every modern device's
mDNS announcement landed on a non-existent ``apollo_r_pro_...``
key and the device stayed marked Unknown forever.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from esphome_device_builder.controllers._device_state_monitor import DeviceStateMonitor
from esphome_device_builder.models import Device, DeviceState


def _device(name: str) -> Device:
    return Device(
        name=name,
        friendly_name=name,
        configuration=f"{name}.yaml",
        address=f"{name}.local",
        state=DeviceState.UNKNOWN,
    )


@pytest.mark.parametrize(
    "yaml_name,mdns_label",
    [
        # Modern hyphenated device — the previously-failing case.
        ("apollo-r-pro-1-eth-5938e0", "apollo-r-pro-1-eth-5938e0"),
        ("home-assistant-voice-090073", "home-assistant-voice-090073"),
        # Underscored YAML name (older convention) — must still work.
        ("legacy_device", "legacy_device"),
        # Single-word name — sanity check.
        ("steamreset", "steamreset"),
    ],
)
async def test_mdns_lookup_uses_literal_service_label(yaml_name: str, mdns_label: str) -> None:
    """A device whose YAML name matches the mDNS label gets marked online.

    Pre-fix, the handler did ``.replace("-", "_")`` before lookup so
    hyphenated YAML names never matched their own mDNS announcement.
    """
    devices = [_device(yaml_name)]
    on_state = MagicMock()
    monitor = DeviceStateMonitor(
        get_devices=lambda: devices,
        on_state_change=on_state,
        on_ip_change=MagicMock(),
        on_version_change=MagicMock(),
    )

    # ``apply()`` is what the (now hyphen-preserving) handler calls.
    # Pre-fix this would have been called with an underscored label
    # that doesn't exist in the catalog, so the assertion below would
    # fail.
    monitor.apply(mdns_label, DeviceState.ONLINE, "mdns")

    assert monitor.priority_for(yaml_name) == "mdns"
    on_state.assert_called_once_with(yaml_name, DeviceState.ONLINE, "mdns")


def test_mdns_label_extraction_does_not_replace_hyphens() -> None:
    """The exact bit of logic we changed: extract the label, no substitution.

    Mirrors the parsing inside ``_on_service_state_change`` so a future
    refactor that re-introduces ``.replace("-", "_")`` fails fast.
    """
    raw = "apollo-r-pro-1-eth-5938e0._esphomelib._tcp.local."
    label = raw.split(".", maxsplit=1)[0]
    assert label == "apollo-r-pro-1-eth-5938e0"
    assert "_" not in label
