"""Pin ``_augment_rp2040_onboard_ethernet_pins`` overlaying eth onto the Pico pinout."""

from __future__ import annotations

from esphome_device_builder.models import (
    BoardCatalogEntry,
    BoardEsphomeConfig,
    BoardPin,
    FeaturedComponent,
    Platform,
)
from esphome_device_builder.models.common import FieldPreset, PinFeature
from script.sync_boards import _augment_rp2040_onboard_ethernet_pins


def _base(board: str, mcu: str) -> BoardCatalogEntry:
    """Return a canonical Pico base board: GPIO0-29, LED on 25."""
    pins = [
        BoardPin(
            gpio=g,
            label=f"GPIO{g}",
            features=[PinFeature.PWM],
            occupied_by="Built-in LED" if g == 25 else None,
        )
        for g in range(30)
    ]
    return BoardCatalogEntry(
        id=board,
        name=board,
        description="d",
        manufacturer="",
        esphome=BoardEsphomeConfig(platform=Platform.RP2040, board=board, mcu=mcu),
        pins=pins,
    )


def _eth_board(*, board: str, mcu: str, fields: dict[str, str]) -> BoardCatalogEntry:
    return BoardCatalogEntry(
        id=board,
        name=board,
        description="d",
        manufacturer="",
        esphome=BoardEsphomeConfig(platform=Platform.RP2040, board=board, mcu=mcu),
        featured_components=[
            FeaturedComponent(
                id="onboard_ethernet",
                component_id="ethernet",
                fields={k: FieldPreset(value=v, locked=True) for k, v in fields.items()},
            )
        ],
        pins=[],
    )


_W6300 = {
    "type": "W6300",
    "clk_pin": "GPIO17",
    "mosi_pin": "GPIO18",
    "miso_pin": "GPIO19",
    "cs_pin": "GPIO16",
    "interrupt_pin": "GPIO15",
    "reset_pin": "GPIO22",
}


def test_overlays_full_pinout_with_ethernet_locked() -> None:
    """A pinless rp2040 eth board gets the 30-pin base with its six SPI pins locked."""
    board = _eth_board(board="w", mcu="rp2040", fields=_W6300)
    _augment_rp2040_onboard_ethernet_pins([_base("rpipico", "rp2040"), board])
    assert len(board.pins) == 30
    occ = {p.gpio: p.occupied_by for p in board.pins if p.occupied_by}
    assert occ == {
        15: "Ethernet INT",
        16: "Ethernet CS",
        17: "Ethernet CLK",
        18: "Ethernet MOSI",
        19: "Ethernet MISO",
        22: "Ethernet RESET",
        25: "Built-in LED",
    }
    locked = next(p for p in board.pins if p.gpio == 17)
    assert locked.available is False and locked.features == []


def test_gpio0_ethernet_pin_is_locked() -> None:
    """A pin field resolving to GPIO0 is locked, not dropped by a falsy check."""
    board = _eth_board(board="w", mcu="rp2040", fields={**_W6300, "cs_pin": "GPIO0"})
    _augment_rp2040_onboard_ethernet_pins([_base("rpipico", "rp2040"), board])
    pin0 = next(p for p in board.pins if p.gpio == 0)
    assert pin0.occupied_by == "Ethernet CS"
    assert pin0.available is False


def test_picks_base_by_mcu() -> None:
    """An rp2350 board overlays onto the rpipico2 base, not rpipico."""
    board = _eth_board(board="w2", mcu="rp2350", fields=_W6300)
    base40 = _base("rpipico", "rp2040")
    base50 = _base("rpipico2", "rp2350")
    base50.pins = base50.pins[:20]  # distinct length to prove which base was used
    _augment_rp2040_onboard_ethernet_pins([base40, base50, board])
    assert len(board.pins) == 20


def test_ethernet_pin_overrides_base_led() -> None:
    """W55RP20's reset on GPIO25 wins over the base Built-in LED there."""
    fields = {**_W6300, "reset_pin": "GPIO25"}
    board = _eth_board(board="w55", mcu="rp2040", fields=fields)
    _augment_rp2040_onboard_ethernet_pins([_base("rpipico", "rp2040"), board])
    assert next(p for p in board.pins if p.gpio == 25).occupied_by == "Ethernet RESET"


def test_board_with_curated_pins_untouched() -> None:
    """A board already shipping pins is left alone (gate on empty pins)."""
    board = _eth_board(board="w", mcu="rp2040", fields=_W6300)
    board.pins = [BoardPin(gpio=2, label="GPIO2")]
    _augment_rp2040_onboard_ethernet_pins([_base("rpipico", "rp2040"), board])
    assert [p.gpio for p in board.pins] == [2]


def test_non_ethernet_rp2040_board_skipped() -> None:
    """A pinless rp2040 board with no ethernet component is not filled."""
    board = BoardCatalogEntry(
        id="x",
        name="x",
        description="d",
        manufacturer="",
        esphome=BoardEsphomeConfig(platform=Platform.RP2040, board="x", mcu="rp2040"),
        pins=[],
    )
    _augment_rp2040_onboard_ethernet_pins([_base("rpipico", "rp2040"), board])
    assert board.pins == []
