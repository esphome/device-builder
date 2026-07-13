"""Tests for the local image path guard in ``script/validate_definitions.py``."""

from __future__ import annotations

from script.validate_definitions import _validate_image_paths  # type: ignore[import-not-found]


def test_clean_manifest_passes() -> None:
    data = {
        "images": ["images/top.png", "https://example.test/x.jpg"],
        "featured_components": [{"id": "a", "image_url": "images/a.png"}],
        "featured_bundles": [{"id": "b", "image_url": "https://example.test/b.jpg"}],
    }
    assert _validate_image_paths("demo", data) == []


def test_escaping_images_entry_rejected() -> None:
    errors = _validate_image_paths("demo", {"images": ["../../../script/x", "/etc/passwd"]})
    assert errors == [
        "demo: images entry '../../../script/x' must be a relative path inside the board dir",
        "demo: images entry '/etc/passwd' must be a relative path inside the board dir",
    ]


def test_escaping_featured_image_url_rejected() -> None:
    data = {
        "featured_components": [{"id": "a", "image_url": "../other/x.png"}],
        "featured_bundles": [{"id": "b", "image_url": "/abs.png"}],
    }
    errors = _validate_image_paths("demo", data)
    assert errors == [
        "demo: featured_components image_url '../other/x.png' must be a relative "
        "path inside the board dir",
        "demo: featured_bundles image_url '/abs.png' must be a relative path inside the board dir",
    ]


def test_non_string_and_absent_values_ignored() -> None:
    data = {"featured_components": [{"id": "a"}, {"id": "b", "image_url": None}]}
    assert _validate_image_paths("demo", data) == []
