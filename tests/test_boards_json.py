"""Drift check: ``boards.json`` must match what the YAML manifests produce."""

from __future__ import annotations

from pathlib import Path

import orjson

from esphome_device_builder.definitions import (
    build_board_catalog_from_manifests,
    load_board_catalog,
)

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
    """Empty-default fields are stripped on serialisation, not on the wire.

    Pins the omit_default config on the board-catalog dataclasses:
    a regression that drops the config would have boards.json grow
    back by ~36% and every WS ``boards/get_boards`` response carry
    the empty fields. The two values pinned here are the highest-
    occurrence ones (``suggestions=None`` is 100% empty across the
    catalog; ``locked=False`` is 81%); their presence in raw bytes
    is sufficient evidence the strip stopped working.
    """
    raw = _BOARDS_JSON.read_text()
    assert '"suggestions":null' not in raw
    assert '"suggestions": null' not in raw
    assert '"locked":false' not in raw
    assert '"locked": false' not in raw
    # The ``id`` field is required (no default) so it survives the
    # strip — sanity-check that the file still has board content
    # rather than an accidentally-empty regeneration.
    payload = orjson.loads(raw)
    assert len(payload["boards"]) > 100
