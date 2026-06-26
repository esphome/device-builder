"""Pin ``_extract_ethernet`` mining a top-level ethernet block into a locked preset."""

from __future__ import annotations

from script.sync_esphome_devices import _extract_ethernet  # type: ignore[import-not-found]


def test_extracts_rmii_and_marks_pins_occupied() -> None:
    """An RMII block (legacy clk_mode) lifts PHY fields and occupies MDC/MDIO/CLK."""
    entry, occ = _extract_ethernet(
        {
            "ethernet": {
                "type": "LAN8720",
                "mdc_pin": "GPIO23",
                "mdio_pin": "GPIO18",
                "clk_mode": "GPIO17_OUT",
                "phy_addr": 0,
            }
        }
    )
    assert entry is not None
    assert entry["component_id"] == "ethernet"
    assert entry["fields"]["type"] == {"value": "LAN8720", "locked": True}
    assert occ == {23: "Ethernet MDC", 18: "Ethernet MDIO", 17: "Ethernet CLK"}


def test_extracts_spi_pins() -> None:
    """A W5500 SPI block occupies each SPI pin (integer GPIOs supported)."""
    _, occ = _extract_ethernet(
        {
            "ethernet": {
                "type": "W5500",
                "clk_pin": 18,
                "mosi_pin": 19,
                "miso_pin": 16,
                "cs_pin": 17,
                "interrupt_pin": 21,
                "reset_pin": 20,
            }
        }
    )
    assert occ == {
        18: "Ethernet CLK",
        19: "Ethernet MOSI",
        16: "Ethernet MISO",
        17: "Ethernet CS",
        21: "Ethernet INT",
        20: "Ethernet RESET",
    }


def test_drops_network_and_runtime_fields() -> None:
    """Site-specific fields (manual_ip / domain / use_address) are not locked."""
    entry, _ = _extract_ethernet(
        {
            "ethernet": {
                "type": "LAN8720",
                "mdc_pin": "GPIO23",
                "domain": ".local",
                "use_address": "device.local",
                "manual_ip": {"static_ip": "10.0.0.5"},
            }
        }
    )
    assert entry is not None
    assert set(entry["fields"]) == {"type", "mdc_pin"}


def test_skips_templated_value() -> None:
    """A ${...} template anywhere in the block skips extraction (never lock unresolved)."""
    entry, occ = _extract_ethernet({"ethernet": {"type": "LAN8720", "mdc_pin": "${eth_mdc}"}})
    assert entry is None
    assert occ == {}


def test_none_when_no_block_or_no_type() -> None:
    """No ethernet block, or a block missing the PHY type, yields nothing."""
    assert _extract_ethernet({"wifi": {}}) == (None, {})
    assert _extract_ethernet({"ethernet": {"mdc_pin": "GPIO23"}}) == (None, {})


def test_skips_block_when_pin_not_concrete_gpio() -> None:
    """A pin field that can't resolve to a GPIO (e.g. !secret) skips the whole block."""
    entry, occ = _extract_ethernet(
        {"ethernet": {"type": "LAN8720", "mdc_pin": "!secret eth_mdc", "mdio_pin": "GPIO18"}}
    )
    assert entry is None
    assert occ == {}


def test_skips_block_when_nested_clk_pin_not_concrete() -> None:
    """A nested clk: with a non-GPIO pin skips the block too."""
    entry, _ = _extract_ethernet(
        {"ethernet": {"type": "LAN8720", "clk": {"pin": "!secret clk", "mode": "CLK_OUT"}}}
    )
    assert entry is None
