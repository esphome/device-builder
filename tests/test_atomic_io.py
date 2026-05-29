"""Tests for the shared :mod:`helpers.atomic_io` write primitive."""

from __future__ import annotations

import os
import stat
import sys
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


@pytest.mark.skipif(sys.platform == "win32", reason="Windows doesn't honor POSIX mode bits")
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


def test_atomic_write_make_parents_creates_missing_dirs(tmp_path: Path) -> None:
    """``make_parents=True`` creates the target's missing ancestor dirs first."""
    target = tmp_path / "a" / "b" / "demo.bin"
    atomic_write(target, b"payload", make_parents=True)
    assert target.read_bytes() == b"payload"


def test_atomic_write_without_make_parents_raises_on_missing_dir(tmp_path: Path) -> None:
    """Without ``make_parents`` a missing target directory surfaces as an error."""
    target = tmp_path / "missing" / "demo.bin"
    with pytest.raises(OSError):
        atomic_write(target, b"payload")
    assert not target.exists()


def test_atomic_write_closes_fd_when_fdopen_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A failure in ``os.fdopen`` doesn't leak the raw fd from ``mkstemp``.

    ``os.fdopen`` is the bridge between the int fd ``mkstemp`` hands
    back and the buffered writer the rest of the body uses. If it
    raises (rare in practice; ENOMEM, invalid fd) before the
    ``with`` enters, nothing closes the fd unless ``atomic_write``
    does so explicitly. Pin the explicit close so a future
    refactor can't silently reintroduce the leak.
    """
    target = tmp_path / "demo.bin"

    closed: list[int] = []
    real_close = os.close

    def _tracking_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    def _failing_fdopen(fd: int, *args: object, **kwargs: object) -> object:
        msg = "no memory"
        raise OSError(msg)

    monkeypatch.setattr("esphome_device_builder.helpers.atomic_io.os.fdopen", _failing_fdopen)
    monkeypatch.setattr("esphome_device_builder.helpers.atomic_io.os.close", _tracking_close)

    with pytest.raises(OSError, match="no memory"):
        atomic_write(target, b"payload")

    # Real fdopen would have consumed and owned the fd, but our
    # failing stub didn't, so the explicit close path must have
    # fired exactly once.
    assert len(closed) == 1, f"expected one explicit os.close, got {closed}"
    assert not target.exists()
    assert not list(tmp_path.glob("demo.bin.*.tmp"))
