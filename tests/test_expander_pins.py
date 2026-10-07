"""Tests for ``script/_expander_pins.py``."""

from __future__ import annotations

from typing import Any

import pytest

from script._expander_pins import expander_hub_ref, is_address_ref
from script.sync_boards import _canonical_pin


@pytest.mark.parametrize(
    ("hub", "expected"),
    [
        ("hub_a", "hub_a"),
        ("", None),
        ({"address": 0x44}, "@0x44"),
        ({"address": "0x44"}, "@0x44"),
        ({"address": "0X0a"}, "@0x0a"),
        ({"address": "68"}, "@0x44"),
        ({"address": "010"}, "@0x0a"),
        ({"address": 0xFF}, "@0xff"),
        ({"address": 0x100}, None),
        ({"address": -1}, None),
        ({"address": "abc"}, None),
        ({"address": None}, None),
        ({"address": 0x44, "id": "x"}, None),
        ({}, None),
        (0x44, None),
        (None, None),
    ],
)
def test_expander_hub_ref(hub: Any, expected: str | None) -> None:
    """Hub ids pass through; address selectors normalise to ``@0x..``."""
    assert expander_hub_ref(hub) == expected


@pytest.mark.parametrize(("ref", "expected"), [("@0x44", True), ("hub_a", False)])
def test_is_address_ref(ref: str, expected: bool) -> None:
    """Only the ``@`` form is an address ref."""
    assert is_address_ref(ref) is expected


@pytest.mark.parametrize(
    ("hub", "expected"),
    [
        ({"address": 0x44}, "pi4ioe5v6408:@0x44:4"),
        ({"address": "0x44"}, "pi4ioe5v6408:@0x44:4"),
        ({"bogus": 1}, None),
    ],
)
def test_canonical_pin_address_selected_expander(hub: dict, expected: str | None) -> None:
    """An address-selected expander pin gets the ``@0x..`` hub token, never a board GPIO."""
    assert _canonical_pin({"pi4ioe5v6408": hub, "number": 4}) == expected
