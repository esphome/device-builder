"""Coverage for ``helpers.paths.resolve_under_root``."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from esphome_device_builder.helpers.paths import PathEscapeError, resolve_under_root

_symlinks = pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges")


def test_returns_resolved_target_inside_root(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    target = tmp_path / "sub" / ".." / "sub" / "file.yaml"
    assert resolve_under_root(target, tmp_path) == (tmp_path / "sub" / "file.yaml").resolve()


def test_root_itself_passes(tmp_path: Path) -> None:
    assert resolve_under_root(tmp_path, tmp_path) == tmp_path.resolve()


def test_dotdot_traversal_raises(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(PathEscapeError):
        resolve_under_root(root / ".." / "outside.yaml", root)


def test_absolute_target_outside_root_raises(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(PathEscapeError):
        resolve_under_root(tmp_path / "elsewhere.yaml", root)


@_symlinks
def test_symlink_pointing_outside_root_raises(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)
    with pytest.raises(PathEscapeError):
        resolve_under_root(root / "link" / "file.yaml", root)


@_symlinks
def test_symlinked_dir_inside_root_resolves(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "real").mkdir(parents=True)
    (root / "alias").symlink_to(root / "real")
    assert resolve_under_root(root / "alias" / "f.bin", root) == root.resolve() / "real" / "f.bin"


def test_unresolvable_target_fails_closed(tmp_path: Path) -> None:
    """An embedded NUL makes ``resolve()`` raise; that must surface as an escape."""
    with pytest.raises(PathEscapeError):
        resolve_under_root(tmp_path / "bad\x00name.yaml", tmp_path)


def test_resolve_value_error_fails_closed(tmp_path: Path) -> None:
    """A non-NUL ``ValueError`` out of ``resolve()`` also surfaces as an escape."""

    class _UnresolvablePath(type(tmp_path)):  # type: ignore[misc]
        def resolve(self, strict: bool = False) -> Path:
            raise ValueError("boom")

    with pytest.raises(PathEscapeError, match="unresolvable"):
        resolve_under_root(_UnresolvablePath(tmp_path, "x"), tmp_path)


def test_escape_error_is_a_value_error(tmp_path: Path) -> None:
    """Callers with an existing ``except ValueError`` contract keep working."""
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(ValueError, match="resolves outside"):
        resolve_under_root(root / ".." / "x", root)
