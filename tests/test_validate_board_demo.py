"""Board ``demo.yaml`` checks in ``script/validate_definitions.py``."""

from __future__ import annotations

from pathlib import Path

from script.validate_definitions import _validate_demo  # type: ignore[import-not-found]


def test_misnamed_demo_is_flagged(tmp_path: Path) -> None:
    """A ``demo.yml`` or other ``*.yml`` next to the manifest is reported, ``demo.yaml`` isn't."""
    for name in ("manifest.yaml", "demo.yaml", "demo.yml", "extra.yml", "logo.svg"):
        (tmp_path / name).write_text("", encoding="utf-8")

    assert _validate_demo("b", tmp_path, {}) == [
        "b: demo.yml is not loaded; name the demo demo.yaml",
        "b: extra.yml is not loaded; name the demo demo.yaml",
    ]


def test_miscased_demo_is_flagged(tmp_path: Path) -> None:
    """A ``Demo.yaml`` alone is reported (its own dir: a case-insensitive FS folds names)."""
    (tmp_path / "Demo.yaml").write_text("", encoding="utf-8")

    assert _validate_demo("b", tmp_path, {}) == [
        "b: Demo.yaml is not loaded; name the demo demo.yaml"
    ]


def test_demo_on_package_board_is_flagged(tmp_path: Path) -> None:
    """A package board's create ignores its demo, so shipping one is an error."""
    (tmp_path / "demo.yaml").write_text("", encoding="utf-8")

    assert _validate_demo("b", tmp_path, {"package_import_url": "github://x/y.yaml"}) == [
        "b: demo.yaml is ignored on a package_import_url board"
    ]
    assert _validate_demo("b", tmp_path, {}) == []
