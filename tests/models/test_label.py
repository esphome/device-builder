"""Tests for the ``Label`` dataclass round-trip.

The ``Label`` model is the wire shape exposed via ``labels/list``
and the persisted shape under ``_labels`` in
``.device-builder.json``. The frontend joins device label-id
references against this list to render colored chips, so a silent
serialization regression — say ``color: None`` round-tripping as
the literal string ``"None"`` — would put junk on every chip.
Mashumaro handles the encode/decode but the test pins the contract
so a future field rename or default change can't slip through.
"""

from __future__ import annotations

from esphome_device_builder.models import Label


def test_label_to_dict_round_trip() -> None:
    """``Label.from_dict(label.to_dict())`` reproduces the original."""
    label = Label(id="abc123", name="Kitchen", color="#ff0000")
    assert Label.from_dict(label.to_dict()) == label


def test_label_color_optional_round_trips_as_none() -> None:
    """``color=None`` survives a dict round-trip (not coerced to ``""``)."""
    label = Label(id="xyz789", name="Dev", color=None)
    encoded = label.to_dict()
    assert encoded["color"] is None
    assert Label.from_dict(encoded) == label


def test_label_to_dict_shape_is_stable() -> None:
    """The persisted keys are exactly id / name / color.

    Pins the wire contract — the frontend reads these keys
    directly off events and ``labels/list`` responses, so adding
    or renaming a field needs an explicit, coordinated change.
    """
    label = Label(id="abc123", name="Kitchen", color="#ff0000")
    assert set(label.to_dict().keys()) == {"id", "name", "color"}
