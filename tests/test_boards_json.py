"""Drift check: ``boards.json`` must match what the YAML manifests produce."""

from __future__ import annotations

from pathlib import Path

import orjson

from esphome_device_builder.definitions import (
    build_board_catalog_from_manifests,
    load_board_catalog,
)
from esphome_device_builder.models.common import FieldPreset

_BOARDS_JSON = (
    Path(__file__).parent.parent / "esphome_device_builder" / "definitions" / "boards.json"
)


def test_boards_json_matches_manifests() -> None:
    """``boards.json`` must be the faithful product of the YAML manifests."""
    from_yaml = build_board_catalog_from_manifests(strict=True)
    from_json = load_board_catalog()

    # Comparing ``to_dict`` rather than dataclass identity gives a
    # readable key-path diff in the assertion message on failure.
    assert from_yaml.to_dict() == from_json.to_dict(), (
        "boards.json is out of sync with the YAML manifests. "
        "Run `python script/sync_boards.py` to regenerate."
    )


def test_boards_json_omits_default_fields() -> None:
    """Empty ``suggestions`` / ``locked`` default rows are stripped from ``boards.json``."""
    # ``encoding="utf-8"`` is load-bearing on Windows: the file
    # carries em-dashes and other non-ASCII chars, and
    # ``Path.read_text`` defaults to the platform encoding (cp1252
    # on the windows-latest CI runner), which trips on the first
    # 0x90 byte from a u'—'.
    raw = _BOARDS_JSON.read_text(encoding="utf-8")
    assert '"suggestions":null' not in raw
    assert '"suggestions": null' not in raw
    assert '"locked":false' not in raw
    assert '"locked": false' not in raw
    # The ``id`` field is required (no default) so it survives the
    # strip — sanity-check that the file still has board content
    # rather than an accidentally-empty regeneration.
    payload = orjson.loads(raw)
    assert len(payload["boards"]) > 100


def test_omit_default_preserves_meaningful_falsy() -> None:
    """``locked=True`` / falsy non-default ``value`` survive the strip."""
    # ``omit_default`` removes a field only when its runtime value
    # equals the *declared* default. ``FieldPreset.value`` defaults
    # to ``None``, so meaningful ``False`` / ``0`` / ``""`` survive
    # — and ``locked=True`` survives because the declared default
    # is ``False``. The board catalog leans on this asymmetry; pin
    # it so a future "make every preset field optional" sweep
    # doesn't silently break the wire shape.
    assert FieldPreset(value=False).to_dict() == {"value": False}
    assert FieldPreset(value=0).to_dict() == {"value": 0}
    assert FieldPreset(value="").to_dict() == {"value": ""}
    assert FieldPreset(value=5, locked=True).to_dict() == {"value": 5, "locked": True}
    # All-defaults round-trips to an empty dict (the strip's whole point).
    assert FieldPreset().to_dict() == {}
