"""Board ``demo.yaml`` checks in ``script/validate_definitions.py``."""

from __future__ import annotations

from pathlib import Path

from script.validate_definitions import _validate_demo  # type: ignore[import-not-found]


def test_misnamed_demo_is_flagged(tmp_path: Path) -> None:
    """A ``demo.yml`` / ``Demo.yaml`` next to the manifest is reported, ``demo.yaml`` isn't."""
    for name in ("manifest.yaml", "demo.yaml", "demo.yml", "Demo.yaml", "logo.svg"):
        (tmp_path / name).write_text("", encoding="utf-8")

    errors = _validate_demo("b", tmp_path, {})

    assert errors == [
        "b: Demo.yaml is not loaded; name the demo demo.yaml",
        "b: demo.yml is not loaded; name the demo demo.yaml",
    ]


def test_demo_on_package_board_is_flagged(tmp_path: Path) -> None:
    """A package board's create ignores its demo, so shipping one is an error."""
    (tmp_path / "demo.yaml").write_text("", encoding="utf-8")

    assert _validate_demo("b", tmp_path, {"package_import_url": "github://x/y.yaml"}) == [
        "b: demo.yaml is ignored on a package_import_url board"
    ]
    assert _validate_demo("b", tmp_path, {}) == []
