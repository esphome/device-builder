"""Tests for the shared :mod:`helpers.atomic_io` write primitive."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from esphome_device_builder.helpers.atomic_io import atomic_write


def test_atomic_write_cleans_up_tempfile_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A crash mid-write leaves no leftover ``.tmp`` files in the config dir.

    ``atomic_write`` stages bytes in ``mkstemp(prefix=name + ".",
    suffix=".tmp", dir=parent)`` and ``os.replace``s into place. If
    ``os.replace`` raises (disk full, permissions, ...) the tempfile
    must be unlinked rather than accumulating one ``.<name>.<random>.tmp``
    file per failed write across the dashboard's lifetime.
    """
    target = tmp_path / "demo.bin"

    def _fail(*args: object, **kwargs: object) -> None:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr("os.replace", _fail)

    with pytest.raises(OSError, match="disk full"):
        atomic_write(target, b"payload")

    assert not target.exists()
    assert not list(tmp_path.glob("demo.bin.*.tmp"))


def test_atomic_write_applies_mode(tmp_path: Path) -> None:
    """The ``mode`` kwarg lands on the destination file."""
    target = tmp_path / "demo.bin"
    atomic_write(target, b"payload", mode=0o600)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.read_bytes() == b"payload"


def test_atomic_write_overwrites_existing(tmp_path: Path) -> None:
    """An existing destination is replaced atomically with the new bytes."""
    target = tmp_path / "demo.bin"
    target.write_bytes(b"old")
    atomic_write(target, b"new")
    assert target.read_bytes() == b"new"
