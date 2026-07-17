"""Coverage for script/bump_esphome_floor.py: stable-only, forward-only floor bumps."""

from __future__ import annotations

from pathlib import Path

import pytest

from script.bump_esphome_floor import bump_floor

_PYPROJECT = """\
[project.optional-dependencies]
esphome = [
  "esphome>=2026.6.0",
]
test = [
  "codespell==2.4.3",
]
"""


@pytest.fixture
def pyproject(tmp_path: Path) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(_PYPROJECT)
    return path


def test_newer_stable_bumps(pyproject: Path) -> None:
    assert bump_floor("2026.7.0", pyproject) == "2026.6.0"
    assert '"esphome>=2026.7.0"' in pyproject.read_text(encoding="utf-8")
    assert "2026.6.0" not in pyproject.read_text(encoding="utf-8")


def test_bump_touches_only_the_floor_line(pyproject: Path) -> None:
    bump_floor("2026.7.1", pyproject)
    assert pyproject.read_text(encoding="utf-8") == _PYPROJECT.replace("2026.6.0", "2026.7.1")


def test_prerelease_skipped(pyproject: Path) -> None:
    assert bump_floor("2026.8.0b1", pyproject) is None
    assert pyproject.read_text(encoding="utf-8") == _PYPROJECT


def test_equal_version_skipped(pyproject: Path) -> None:
    assert bump_floor("2026.6.0", pyproject) is None
    assert pyproject.read_text(encoding="utf-8") == _PYPROJECT


def test_older_version_never_lowers(pyproject: Path) -> None:
    assert bump_floor("2026.5.9", pyproject) is None
    assert pyproject.read_text(encoding="utf-8") == _PYPROJECT


def test_numeric_compare_not_lexicographic(pyproject: Path) -> None:
    assert bump_floor("2026.10.0", pyproject) == "2026.6.0"
    assert '"esphome>=2026.10.0"' in pyproject.read_text(encoding="utf-8")


def test_missing_floor_fails_loud(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nname = "x"\n')
    with pytest.raises(SystemExit, match="exactly one"):
        bump_floor("2026.7.0", path)


def test_duplicate_floor_fails_loud(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text(_PYPROJECT + _PYPROJECT)
    with pytest.raises(SystemExit, match="exactly one"):
        bump_floor("2026.7.0", path)
