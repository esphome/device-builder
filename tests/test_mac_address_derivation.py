"""Tests for the per-interface MAC derivation helper.

The mDNS ``mac`` TXT carries the device's primary MAC. ESP32-family
devices that also enable Ethernet / Bluetooth derive those
interfaces' MACs from the same base via fixed offsets per
Espressif's allocation table; RP2040 / RP2350 share a single MAC
across interfaces. The dashboard renders every derived MAC in the
device drawer so users can match a device to its router-side
ethernet MAC, BLE scanner readings, etc. without forcing the
firmware to broadcast each one.

All MACs in / out of :func:`derive_interface_macs` are in the
canonical ``XX:XX:XX:XX:XX:XX`` form that
:func:`controllers._device_state_monitor._normalize_mac` produces.
"""

from __future__ import annotations

import pytest

from esphome_device_builder.helpers.mac_addresses import derive_interface_macs

# ----------------------------------------------------------------------
# ESP32 family — base + offset per Espressif's table
# ----------------------------------------------------------------------


def test_esp32_ethernet_offsets_last_octet_by_three() -> None:
    """``ethernet`` integration → base + 3 to last octet."""
    ethernet, bluetooth = derive_interface_macs("94:C9:60:1F:8C:F0", "esp32", ["ethernet"])
    assert ethernet == "94:C9:60:1F:8C:F3"
    assert bluetooth == ""


def test_esp32_bluetooth_offsets_last_octet_by_two() -> None:
    """Any ``esp32_ble*`` integration → base + 2 to last octet."""
    ethernet, bluetooth = derive_interface_macs("94:C9:60:1F:8C:F0", "esp32", ["esp32_ble_tracker"])
    assert ethernet == ""
    assert bluetooth == "94:C9:60:1F:8C:F2"


def test_esp32_bluetooth_matches_bluetooth_proxy_prefix() -> None:
    """``bluetooth_proxy`` (no ``esp32_`` prefix) still flips the bit."""
    ethernet, bluetooth = derive_interface_macs("94:C9:60:1F:8C:F0", "esp32", ["bluetooth_proxy"])
    assert ethernet == ""
    assert bluetooth == "94:C9:60:1F:8C:F2"


def test_esp32_both_integrations_derive_both() -> None:
    """A device with ethernet + bluetooth surfaces both derived MACs."""
    ethernet, bluetooth = derive_interface_macs(
        "94:C9:60:1F:8C:F0",
        "esp32",
        ["api", "ethernet", "esp32_ble_tracker", "wifi"],
    )
    assert ethernet == "94:C9:60:1F:8C:F3"
    assert bluetooth == "94:C9:60:1F:8C:F2"


def test_esp32_no_integrations_yields_empty() -> None:
    """A pure Wi-Fi device with no extras has no derived MACs."""
    ethernet, bluetooth = derive_interface_macs("94:C9:60:1F:8C:F0", "esp32", ["api", "wifi"])
    assert ethernet == ""
    assert bluetooth == ""


def test_esp32_last_octet_overflow_wraps_modulo_256() -> None:
    """``0xFF`` + 3 → ``0x02``; the upper octets stay put.

    Mirrors ESP-IDF behaviour where the offset is added with
    modular wrapping rather than carrying into the next byte.
    """
    ethernet, _ = derive_interface_macs("94:C9:60:1F:8C:FF", "esp32", ["ethernet"])
    assert ethernet == "94:C9:60:1F:8C:02"


@pytest.mark.parametrize(
    "platform",
    ["esp32s2", "esp32s3", "esp32c3", "esp32c6", "esp32h2", "esp32p4"],
)
def test_esp32_variants_use_same_offsets(platform: str) -> None:
    """ESP32 variants share the eFuse layout and offset table."""
    ethernet, bluetooth = derive_interface_macs(
        "94:C9:60:1F:8C:F0", platform, ["ethernet", "esp32_ble"]
    )
    assert ethernet == "94:C9:60:1F:8C:F3"
    assert bluetooth == "94:C9:60:1F:8C:F2"


# ----------------------------------------------------------------------
# Single-MAC platforms — RP2040 / RP2350 share one MAC across interfaces
# ----------------------------------------------------------------------


def test_rp2040_ethernet_equals_primary() -> None:
    """W5500-on-RP2040 reuses the single platform MAC."""
    ethernet, bluetooth = derive_interface_macs("94:C9:60:1F:8C:F0", "rp2040", ["ethernet"])
    assert ethernet == "94:C9:60:1F:8C:F0"
    # No bluetooth derivation: Pico W's BT lives on the CYW43439
    # with its own MAC the dashboard can't compute from RP-side
    # data.
    assert bluetooth == ""


def test_rp2350_ethernet_equals_primary() -> None:
    """RP2350 follows the same single-MAC scheme as RP2040."""
    ethernet, bluetooth = derive_interface_macs("94:C9:60:1F:8C:F0", "rp2350", ["ethernet"])
    assert ethernet == "94:C9:60:1F:8C:F0"
    assert bluetooth == ""


def test_rp2040_no_ethernet_yields_empty() -> None:
    """Without the ``ethernet`` integration the row stays hidden."""
    ethernet, bluetooth = derive_interface_macs("94:C9:60:1F:8C:F0", "rp2040", ["api", "wifi"])
    assert ethernet == ""
    assert bluetooth == ""


def test_rp2_renamed_platform_follows_single_mac_scheme() -> None:
    """The renamed ``rp2`` key derives like ``rp2040``."""
    ethernet, bluetooth = derive_interface_macs("94:C9:60:1F:8C:F0", "rp2", ["ethernet"])
    assert ethernet == "94:C9:60:1F:8C:F0"
    assert bluetooth == ""
    no_eth, _ = derive_interface_macs("94:C9:60:1F:8C:F0", "rp2", ["api", "wifi"])
    assert no_eth == ""


# ----------------------------------------------------------------------
# Edge cases — empty / malformed / unknown inputs
# ----------------------------------------------------------------------


def test_empty_primary_yields_empty() -> None:
    """No primary MAC observed yet → no derivation."""
    assert derive_interface_macs("", "esp32", ["ethernet"]) == ("", "")


def test_short_primary_yields_empty() -> None:
    """A primary that's not the canonical 17-char form short-circuits before any math."""
    assert derive_interface_macs("94:C9:60", "esp32", ["ethernet"]) == ("", "")


def test_correct_length_non_hex_primary_yields_empty() -> None:
    """A 17-char value with non-hex chars in the last octet is rejected.

    A corrupt or hand-edited sidecar entry could end up the right
    length but carry junk in the trailing octet ``ZZ``. Without an
    explicit hex check the offset math would raise ``ValueError``
    deep inside ``_offset_last_octet``; the helper instead returns
    empty so the caller treats the device as "no derived MACs".
    """
    assert derive_interface_macs("94:C9:60:1F:8C:ZZ", "esp32", ["ethernet"]) == (
        "",
        "",
    )


def test_uncanonical_primary_yields_empty() -> None:
    """Compact 12-hex-char input (the broadcast form) is rejected.

    ``derive_interface_macs`` is wired downstream of
    ``_normalize_mac``; a non-canonical input here means a caller
    skipped the normalization step. Returning empty rather than
    guessing keeps the offset math from operating on
    unvalidated bytes.
    """
    assert derive_interface_macs("94c9601f8cf0", "esp32", ["ethernet"]) == ("", "")


def test_unknown_platform_yields_empty() -> None:
    """A platform we haven't validated (e.g. ``bk72xx``) → no derivation."""
    assert derive_interface_macs("94:C9:60:1F:8C:F0", "bk72xx", ["ethernet", "esp32_ble"]) == (
        "",
        "",
    )


def test_empty_platform_yields_empty() -> None:
    """``target_platform`` blank (never compiled) → no derivation."""
    assert derive_interface_macs("94:C9:60:1F:8C:F0", "", ["ethernet"]) == ("", "")
