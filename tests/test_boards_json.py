"""Drift check: ``boards.json`` must match what the YAML manifests produce."""

from __future__ import annotations

from esphome_device_builder.definitions import (
    build_board_catalog_from_manifests,
    load_board_catalog,
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
