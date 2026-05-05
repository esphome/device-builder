"""Tests for the per-interface MAC derivation helper.

The mDNS ``mac`` TXT carries the device's primary MAC. ESP32-family
devices that also enable Ethernet / Bluetooth derive those
interfaces' MACs from the same base via fixed offsets per
Espressif's allocation table; RP2040 / RP2350 share a single MAC
across interfaces. The dashboard renders every derived MAC in the
device drawer so users can match a device to its router-side
ethernet MAC, BLE scanner readings, etc. without forcing the
firmware to broadcast each one.
"""

from __future__ import annotations

import pytest

from esphome_device_builder.helpers.mac_addresses import derive_interface_macs


class TestEsp32:
    """ESP32 family follows base + offset per Espressif's table."""

    def test_ethernet_offsets_last_octet_by_three(self) -> None:
        """``ethernet`` integration → base + 3 to last octet."""
        ethernet, bluetooth = derive_interface_macs("94c9601f8cf0", "esp32", ["ethernet"])
        assert ethernet == "94c9601f8cf3"
        assert bluetooth == ""

    def test_bluetooth_offsets_last_octet_by_two(self) -> None:
        """Any ``esp32_ble*`` integration → base + 2 to last octet."""
        ethernet, bluetooth = derive_interface_macs("94c9601f8cf0", "esp32", ["esp32_ble_tracker"])
        assert ethernet == ""
        assert bluetooth == "94c9601f8cf2"

    def test_bluetooth_matches_bluetooth_proxy_prefix(self) -> None:
        """``bluetooth_proxy`` (no ``esp32_`` prefix) still flips the bit."""
        ethernet, bluetooth = derive_interface_macs("94c9601f8cf0", "esp32", ["bluetooth_proxy"])
        assert ethernet == ""
        assert bluetooth == "94c9601f8cf2"

    def test_both_integrations_derive_both(self) -> None:
        """A device with ethernet + bluetooth surfaces both derived MACs."""
        ethernet, bluetooth = derive_interface_macs(
            "94c9601f8cf0",
            "esp32",
            ["api", "ethernet", "esp32_ble_tracker", "wifi"],
        )
        assert ethernet == "94c9601f8cf3"
        assert bluetooth == "94c9601f8cf2"

    def test_no_integrations_yields_empty(self) -> None:
        """A pure Wi-Fi device with no extras has no derived MACs."""
        ethernet, bluetooth = derive_interface_macs("94c9601f8cf0", "esp32", ["api", "wifi"])
        assert ethernet == ""
        assert bluetooth == ""

    def test_last_octet_overflow_wraps_modulo_256(self) -> None:
        """``0xff`` + 3 → ``0x02``; the upper octets stay put.

        Mirrors ESP-IDF behaviour where the offset is added with
        modular wrapping rather than carrying into the next byte.
        """
        ethernet, _ = derive_interface_macs("94c9601f8cff", "esp32", ["ethernet"])
        assert ethernet == "94c9601f8c02"

    @pytest.mark.parametrize(
        "platform",
        ["esp32s2", "esp32s3", "esp32c3", "esp32c6", "esp32h2", "esp32p4"],
    )
    def test_other_esp32_variants_use_same_offsets(self, platform: str) -> None:
        """ESP32 variants share the eFuse layout and offset table."""
        ethernet, bluetooth = derive_interface_macs(
            "94c9601f8cf0", platform, ["ethernet", "esp32_ble"]
        )
        assert ethernet == "94c9601f8cf3"
        assert bluetooth == "94c9601f8cf2"


class TestSingleMacPlatforms:
    """RP2040 / RP2350 share one MAC across interfaces."""

    def test_rp2040_ethernet_equals_primary(self) -> None:
        """W5500-on-RP2040 reuses the single platform MAC."""
        ethernet, bluetooth = derive_interface_macs("94c9601f8cf0", "rp2040", ["ethernet"])
        assert ethernet == "94c9601f8cf0"
        # No bluetooth derivation: Pico W's BT lives on the CYW43439
        # with its own MAC the dashboard can't compute from RP-side
        # data.
        assert bluetooth == ""

    def test_rp2350_ethernet_equals_primary(self) -> None:
        ethernet, bluetooth = derive_interface_macs("94c9601f8cf0", "rp2350", ["ethernet"])
        assert ethernet == "94c9601f8cf0"
        assert bluetooth == ""

    def test_rp2040_no_ethernet_yields_empty(self) -> None:
        """Without the ``ethernet`` integration the row stays hidden."""
        ethernet, bluetooth = derive_interface_macs("94c9601f8cf0", "rp2040", ["api", "wifi"])
        assert ethernet == ""
        assert bluetooth == ""


class TestEdgeCases:
    """Empty / malformed / unknown inputs return empty derivations."""

    def test_empty_primary_yields_empty(self) -> None:
        assert derive_interface_macs("", "esp32", ["ethernet"]) == ("", "")

    def test_short_primary_yields_empty(self) -> None:
        """A primary that's not 12 chars short-circuits before any math."""
        assert derive_interface_macs("94c960", "esp32", ["ethernet"]) == ("", "")

    def test_unknown_platform_yields_empty(self) -> None:
        """A platform we haven't validated (e.g. ``bk72xx``) → no derivation."""
        assert derive_interface_macs("94c9601f8cf0", "bk72xx", ["ethernet", "esp32_ble"]) == (
            "",
            "",
        )

    def test_empty_platform_yields_empty(self) -> None:
        """``target_platform`` blank (never compiled) → no derivation."""
        assert derive_interface_macs("94c9601f8cf0", "", ["ethernet"]) == ("", "")
