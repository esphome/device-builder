"""Drift check: ``boards.json`` must match what the YAML manifests produce.

The runtime catalog reads ``definitions/boards.json``; the manifest
YAMLs are the human-editable source. CI also runs
``script/sync_boards.py`` + ``git diff --exit-code`` to catch the same
drift, but this test gives contributors fast local feedback (no
``pre-commit run --all-files`` round-trip needed) and pins the
``mashumaro from_dict`` codepath end-to-end against real fixture data.
"""

from __future__ import annotations

from esphome_device_builder.definitions import (
    build_board_catalog_from_manifests,
    load_board_catalog,
)


def test_boards_json_matches_manifests() -> None:
    """``boards.json`` must be the byte-faithful product of the YAML manifests.

    A drift here means someone edited a ``manifest.yaml`` without
    regenerating the JSON (or added a new model field without
    extending the sync). Either way, the dashboard would ship a stale
    catalog. ``script/sync_boards.py`` is the fix.
    """
    from_yaml = build_board_catalog_from_manifests(strict=True)
    from_json = load_board_catalog()

    # Compare via ``to_dict`` rather than dataclass identity — equality
    # on dataclasses is field-wise, but going through to_dict gives
    # a much more readable diff when it fails (the failing assertion
    # message points at the offending key path).
    assert from_yaml.to_dict() == from_json.to_dict(), (
        "boards.json is out of sync with the YAML manifests. "
        "Run `python script/sync_boards.py` to regenerate."
    )
