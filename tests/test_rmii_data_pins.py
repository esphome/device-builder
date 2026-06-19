"""Pin ``_augment_rmii_data_pins`` marking the fixed RMII data pins occupied."""

from __future__ import annotations

from esphome_device_builder.models import (
    BoardCatalogEntry,
    BoardEsphomeConfig,
    BoardPin,
    Esp32Variant,
    FeaturedComponent,
    Platform,
)
from esphome_device_builder.models.common import FieldPreset, PinFeature
from script.sync_boards import _augment_rmii_data_pins

_RMII_GPIOS = {19, 21, 22, 25, 26, 27}


def _board(
    *,
    platform: Platform = Platform.ESP32,
    variant: Esp32Variant | None = Esp32Variant.ESP32,
    eth_fields: dict[str, object] | None = None,
    pins: list[BoardPin] | None = None,
) -> BoardCatalogEntry:
    featured = []
    if eth_fields is not None:
        featured = [
            FeaturedComponent(
                id="onboard_ethernet",
                component_id="ethernet",
                fields={k: FieldPreset(value=v, locked=True) for k, v in eth_fields.items()},
            )
        ]
    return BoardCatalogEntry(
        id="b",
        name="B",
        description="",
        manufacturer="",
        esphome=BoardEsphomeConfig(platform=platform, board="esp32dev", variant=variant),
        featured_components=featured,
        pins=pins or [],
    )


def test_rmii_board_gains_six_occupied_data_pins() -> None:
    """An esp32 RMII board (mdc_pin present) gets all six fixed data pins occupied."""
    board = _board(eth_fields={"type": "LAN8720", "mdc_pin": "GPIO23"})
    _augment_rmii_data_pins([board])
    occupied = {
        p.gpio for p in board.pins if p.occupied_by and p.occupied_by.startswith("Ethernet")
    }
    assert occupied == _RMII_GPIOS
    assert all(p.available is False for p in board.pins if p.gpio in _RMII_GPIOS)


def test_replaces_free_pin_keeping_label_dropping_features() -> None:
    """A free data pin is flagged occupied; its label survives, features/notes drop."""
    board = _board(
        eth_fields={"type": "LAN8720", "mdc_pin": "GPIO23"},
        pins=[BoardPin(gpio=19, label="GPIO19", features=[PinFeature.PWM], notes="VSPI MISO")],
    )
    _augment_rmii_data_pins([board])
    pin = next(p for p in board.pins if p.gpio == 19)
    assert pin.occupied_by == "Ethernet TXD0"
    assert pin.available is False
    assert pin.label == "GPIO19"
    assert pin.features == []
    assert pin.notes is None


def test_does_not_clobber_existing_occupancy() -> None:
    """A data gpio already occupied by something else is left untouched."""
    board = _board(
        eth_fields={"type": "LAN8720", "mdc_pin": "GPIO23"},
        pins=[BoardPin(gpio=19, occupied_by="Built-in LED")],
    )
    _augment_rmii_data_pins([board])
    assert next(p for p in board.pins if p.gpio == 19).occupied_by == "Built-in LED"


def test_spi_ethernet_board_untouched() -> None:
    """An SPI board (cs_pin, no mdc_pin) gets no RMII data pins."""
    board = _board(eth_fields={"type": "W5500", "cs_pin": "GPIO2"})
    _augment_rmii_data_pins([board])
    assert not any(p.occupied_by and "Ethernet" in p.occupied_by for p in board.pins)


def test_non_esp32_board_skipped() -> None:
    """A non-esp32 board is skipped even with an RMII-looking ethernet block."""
    board = _board(
        platform=Platform.RP2040,
        variant=None,
        eth_fields={"type": "LAN8720", "mdc_pin": "GPIO23"},
    )
    _augment_rmii_data_pins([board])
    assert board.pins == []


def test_esp32p4_board_uses_p4_pins() -> None:
    """An esp32p4 RMII board gets the P4 default data pins, not the classic set."""
    board = _board(
        variant=Esp32Variant.ESP32P4,
        eth_fields={"type": "LAN8720", "mdc_pin": "GPIO23"},
    )
    _augment_rmii_data_pins([board])
    occupied = {
        p.gpio for p in board.pins if p.occupied_by and p.occupied_by.startswith("Ethernet")
    }
    assert occupied == {28, 29, 30, 34, 35, 49}


def test_esp32_variant_without_emac_skipped() -> None:
    """An esp32 variant with no RMII pin map (e.g. esp32s3) is left untouched."""
    board = _board(
        variant=Esp32Variant.ESP32S3,
        eth_fields={"type": "LAN8720", "mdc_pin": "GPIO23"},
    )
    _augment_rmii_data_pins([board])
    assert board.pins == []
