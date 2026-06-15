"""
Tests for the variant-less platforms' board combobox.

Pins that ``board`` surfaces as a board-catalog combobox (options +
``allow_custom_value``) for the platforms whose ``board`` is the required
sole selector (no ``variant``), and stays deprecated for the variant-
bearing platforms (esp32, rp2040). ``_board_options_for_platform`` is
checked against the committed board catalog so an upstream board churn or
a dedupe regression trips here.
"""

from __future__ import annotations

import orjson

from script.sync_components import (  # type: ignore[import-not-found]
    _BOARD_COMBOBOX_PLATFORMS,
    _DEPRECATED_FIELDS,
    _OUTPUT_BODIES_DIR,
    _apply_board_options,
    _board_options_for_platform,
    _load_board_index,
)


def _board_entry(component_id: str) -> dict | None:
    body = orjson.loads((_OUTPUT_BODIES_DIR / f"{component_id}.json").read_bytes())
    for entry in body.get("config_entries", []):
        if entry.get("key") == "board":
            return entry
    return None


def test_deprecated_and_combobox_sets_are_disjoint() -> None:
    """A platform is either variant-driven (deprecated) or board-combobox, never both."""
    deprecated_board = {cid for cid, key in _DEPRECATED_FIELDS if key == "board"}
    assert deprecated_board == {"esp32", "rp2040"}
    assert {"esp8266", "nrf52", "bk72xx", "rtl87xx", "ln882x"} == _BOARD_COMBOBOX_PLATFORMS
    assert deprecated_board.isdisjoint(_BOARD_COMBOBOX_PLATFORMS)


def test_board_options_are_distinct_sorted_pairs() -> None:
    """Options are sorted, deduped ``esphome.board`` ids as ``{label, value}``."""
    options = _board_options_for_platform("rtl87xx")
    values = [o["value"] for o in options]
    assert values == sorted(set(values))  # distinct + sorted
    assert all(o["label"] == o["value"] for o in options)
    assert {"value": "bw15", "label": "bw15"} in options


def test_board_options_dedupe_shared_esphome_board() -> None:
    """esp8266 has many catalog entries sharing one ``esphome.board`` — collapse them."""
    entries = [
        b for b in _load_board_index() if (b.get("esphome") or {}).get("platform") == "esp8266"
    ]
    distinct = {(b.get("esphome") or {}).get("board") for b in entries} - {None}
    options = _board_options_for_platform("esp8266")
    # Self-consistent rather than a hardcoded count: far more catalog entries
    # than distinct board ids, and the helper yields exactly the distinct set.
    assert len(entries) > len(options)
    assert len(options) == len(distinct)


def test_apply_board_options_sets_combobox_on_board_entry() -> None:
    """The board entry gains options + ``allow_custom_value`` for a combobox platform."""
    entries = [{"key": "board", "type": "string"}, {"key": "family", "type": "string"}]
    _apply_board_options("rtl87xx", entries)
    board = entries[0]
    assert board["allow_custom_value"] is True
    assert board["options"] and {"value": "bw15", "label": "bw15"} in board["options"]
    assert "options" not in entries[1]  # untouched


def test_apply_board_options_noop_for_variant_platform() -> None:
    """esp32 keeps board deprecated — even a stray board entry isn't decorated."""
    entries = [{"key": "board", "type": "string"}]
    _apply_board_options("esp32", entries)
    assert "options" not in entries[0]
    assert "allow_custom_value" not in entries[0]


def test_apply_board_options_noop_without_board_entry() -> None:
    """A combobox platform whose entry list has no board key is left untouched."""
    entries = [{"key": "framework", "type": "string"}]
    _apply_board_options("rtl87xx", entries)
    assert entries == [{"key": "framework", "type": "string"}]


def test_shipped_catalog_surfaces_board_combobox() -> None:
    """The generated bodies expose a required board combobox for variant-less platforms."""
    for component_id in ("esp8266", "nrf52", "rtl87xx"):
        board = _board_entry(component_id)
        assert board is not None, f"{component_id} missing board entry"
        assert board["type"] == "string"
        assert board["required"] is True
        assert board["allow_custom_value"] is True
        assert board["options"], f"{component_id} board has no options"


def test_shipped_catalog_keeps_variant_platforms_boardless() -> None:
    """esp32 / rp2040 stay variant-driven — no board entry in the editor catalog."""
    assert _board_entry("esp32") is None
    assert _board_entry("rp2040") is None
