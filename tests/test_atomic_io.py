"""Tests for the shared :mod:`helpers.atomic_io` read and write primitives."""

from __future__ import annotations

import errno
import os
import stat
import sys
from pathlib import Path

import pytest

from esphome_device_builder.helpers.atomic_io import (
    atomic_write,
    atomic_write_exclusive,
    atomic_write_preserving_mode,
    read_bytes_with_retry,
    read_text_with_stat,
)


def test_read_text_with_stat_pairs_content_with_the_handle_stat(tmp_path: Path) -> None:
    """The returned stat describes exactly the bytes read."""
    path = tmp_path / "kitchen.yaml"
    # newline="\n" keeps the on-disk bytes LF-only so the size
    # comparison holds under Windows' \r\n translation.
    path.write_text("esphome:\n  name: kitchen\n", encoding="utf-8", newline="\n")

    file_stat, content = read_text_with_stat(path)

    assert content == "esphome:\n  name: kitchen\n"
    assert file_stat.st_size == len(content.encode())


@pytest.mark.skipif(sys.platform == "win32", reason="cannot replace a file with an open handle")
def test_read_text_with_stat_survives_an_atomic_replace_mid_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replace between open and read yields the pre-replace stat and content together."""
    path = tmp_path / "kitchen.yaml"
    path.write_text("old: 1\n", encoding="utf-8")

    real_open = Path.open

    def _replace_after_open(self: Path, *args: object, **kwargs: object) -> object:
        fh = real_open(self, *args, **kwargs)
        if self.name == "kitchen.yaml":
            staged = tmp_path / "staged.yaml"
            staged.write_text("new: 2 and longer\n", encoding="utf-8")
            staged.replace(path)
        return fh

    monkeypatch.setattr(Path, "open", _replace_after_open)
    file_stat, content = read_text_with_stat(path)

    assert content == "old: 1\n"
    assert file_stat.st_size == len(content.encode())


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


@pytest.mark.skipif(sys.platform == "win32", reason="Windows doesn't honor POSIX mode bits")
@pytest.mark.parametrize("mode", [pytest.param(0o600, id="0600"), pytest.param(0o640, id="0640")])
def test_atomic_write_preserving_mode_keeps_existing_mode(tmp_path: Path, mode: int) -> None:
    """A rewrite keeps the target's current permission bits."""
    target = tmp_path / "secrets.yaml"
    target.write_text("a: 1\n")
    target.chmod(mode)
    atomic_write_preserving_mode(target, b"b: 2\n")
    assert target.read_bytes() == b"b: 2\n"
    assert stat.S_IMODE(target.stat().st_mode) == mode


@pytest.mark.skipif(sys.platform == "win32", reason="Windows doesn't honor POSIX mode bits")
def test_atomic_write_preserving_mode_new_file_gets_default(tmp_path: Path) -> None:
    """A missing target is created with ``default_mode``."""
    target = tmp_path / "secrets.yaml"
    atomic_write_preserving_mode(target, b"a: 1\n")
    assert target.read_bytes() == b"a: 1\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


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
    with pytest.raises(FileNotFoundError):
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


def test_atomic_write_retries_replace_on_windows_handle_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient Windows ``PermissionError`` on rename is retried, not surfaced."""
    monkeypatch.setattr("esphome_device_builder.helpers.atomic_io._IS_WINDOWS", True)
    monkeypatch.setattr("esphome_device_builder.helpers.atomic_io.time.sleep", lambda _s: None)
    target = tmp_path / "demo.bin"
    target.write_bytes(b"old")

    real_replace = os.replace
    calls = {"n": 0}

    def _flaky(src: object, dst: object) -> None:
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(5, "Access is denied")
        real_replace(src, dst)

    monkeypatch.setattr("os.replace", _flaky)
    atomic_write(target, b"new")

    assert calls["n"] == 3  # failed twice, succeeded on the third
    assert target.read_bytes() == b"new"
    assert not list(tmp_path.glob("demo.bin.*.tmp"))


def test_atomic_write_replace_backoff_grows_and_caps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows replace backoff grows exponentially, capped per-sleep.

    Pins the widened retry budget: a slow scanner under loaded CI
    must get several seconds across all retries, not the old flat
    0.5s, while each individual wait stays bounded by the cap.
    """
    monkeypatch.setattr("esphome_device_builder.helpers.atomic_io._IS_WINDOWS", True)
    sleeps: list[float] = []

    def _record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("esphome_device_builder.helpers.atomic_io.time.sleep", _record_sleep)
    target = tmp_path / "demo.bin"
    target.write_bytes(b"old")

    real_replace = os.replace
    calls = {"n": 0}

    def _flaky(src: object, dst: object) -> None:
        calls["n"] += 1
        if calls["n"] < 6:
            raise PermissionError(5, "Access is denied")
        real_replace(src, dst)

    monkeypatch.setattr("os.replace", _flaky)
    atomic_write(target, b"new")

    assert target.read_bytes() == b"new"
    # Exponential growth (0.05 × 2**attempt) capped at 0.5s per wait.
    assert sleeps == [0.05, 0.1, 0.2, 0.4, 0.5]


def test_atomic_write_does_not_retry_replace_on_posix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``PermissionError`` on POSIX is a real error and surfaces immediately."""
    monkeypatch.setattr("esphome_device_builder.helpers.atomic_io._IS_WINDOWS", False)
    calls = {"n": 0}

    def _fail(src: object, dst: object) -> None:
        calls["n"] += 1
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("os.replace", _fail)
    with pytest.raises(PermissionError):
        atomic_write(tmp_path / "demo.bin", b"x")

    assert calls["n"] == 1  # no retry on POSIX


def test_read_bytes_with_retry_returns_contents(tmp_path: Path) -> None:
    target = tmp_path / "demo.bin"
    target.write_bytes(b"payload")
    assert read_bytes_with_retry(target) == b"payload"


def test_read_bytes_with_retry_retries_on_windows_sharing_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient Windows ``PermissionError`` on open is retried, not surfaced."""
    monkeypatch.setattr("esphome_device_builder.helpers.atomic_io._IS_WINDOWS", True)
    monkeypatch.setattr("esphome_device_builder.helpers.atomic_io.time.sleep", lambda _s: None)
    target = tmp_path / "demo.bin"
    target.write_bytes(b"payload")

    real_read_bytes = Path.read_bytes
    calls = {"n": 0}

    def _flaky(self: Path) -> bytes:
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(5, "Access is denied")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _flaky)
    assert read_bytes_with_retry(target) == b"payload"
    assert calls["n"] == 3  # failed twice, succeeded on the third


def test_read_bytes_with_retry_does_not_retry_on_posix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``PermissionError`` on POSIX is real and surfaces without a retry."""
    monkeypatch.setattr("esphome_device_builder.helpers.atomic_io._IS_WINDOWS", False)
    calls = {"n": 0}

    def _fail(self: Path) -> bytes:
        calls["n"] += 1
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "read_bytes", _fail)
    with pytest.raises(PermissionError):
        read_bytes_with_retry(tmp_path / "demo.bin")

    assert calls["n"] == 1  # no retry on POSIX


def test_atomic_write_exclusive_creates_fresh_file(tmp_path: Path) -> None:
    """A fresh target gets the full payload, 0o644, and no staging litter."""
    target = tmp_path / "kitchen.yaml"

    atomic_write_exclusive(target, b"esphome:\n")

    assert target.read_bytes() == b"esphome:\n"
    if sys.platform != "win32":
        assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_exclusive_refuses_existing_target(tmp_path: Path) -> None:
    """An existing target raises FileExistsError, keeps its bytes, leaves no litter."""
    target = tmp_path / "kitchen.yaml"
    target.write_bytes(b"original")

    with pytest.raises(FileExistsError):
        atomic_write_exclusive(target, b"clobber")

    assert target.read_bytes() == b"original"
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_exclusive_failed_publish_leaves_no_partial_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A publish failure surfaces, and neither a partial target nor staging litter remains."""
    target = tmp_path / "kitchen.yaml"

    def _fail(src: object, dst: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("esphome_device_builder.helpers.atomic_io.os.link", _fail)
    monkeypatch.setattr("esphome_device_builder.helpers.atomic_io.os.rename", _fail)

    with pytest.raises(OSError, match="No space left"):
        atomic_write_exclusive(target, b"esphome:\n")

    assert not target.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_exclusive_concurrent_creator_loses_cleanly(tmp_path: Path) -> None:
    """A target appearing after staging (the TOCTOU window) still raises FileExistsError."""
    target = tmp_path / "kitchen.yaml"
    real_link = os.link
    real_rename = os.rename

    def _racing_link(src: object, dst: object, **kwargs: object) -> None:
        target.write_bytes(b"raced")
        real_link(src, dst, **kwargs)  # type: ignore[arg-type]

    def _racing_rename(src: object, dst: object, **kwargs: object) -> None:
        target.write_bytes(b"raced")
        real_rename(src, dst, **kwargs)  # type: ignore[arg-type]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("esphome_device_builder.helpers.atomic_io.os.link", _racing_link)
        mp.setattr("esphome_device_builder.helpers.atomic_io.os.rename", _racing_rename)
        with pytest.raises(FileExistsError):
            atomic_write_exclusive(target, b"clobber")

    assert target.read_bytes() == b"raced"
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_exclusive_retries_windows_scanner_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient Windows ``PermissionError`` on the exclusive publish is retried."""
    monkeypatch.setattr("esphome_device_builder.helpers.atomic_io._IS_WINDOWS", True)
    monkeypatch.setattr("esphome_device_builder.helpers.atomic_io.time.sleep", lambda _s: None)
    target = tmp_path / "kitchen.yaml"

    real_rename = os.rename
    calls = {"n": 0}

    def _flaky(src: object, dst: object, **kwargs: object) -> None:
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(5, "Access is denied")
        real_rename(src, dst, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("os.rename", _flaky)
    atomic_write_exclusive(target, b"esphome:\n")

    assert calls["n"] == 3  # failed twice, succeeded on the third
    assert target.read_bytes() == b"esphome:\n"
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_exclusive_windows_existing_target_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``FileExistsError`` on the Windows publish surfaces immediately, no retry."""
    monkeypatch.setattr("esphome_device_builder.helpers.atomic_io._IS_WINDOWS", True)
    target = tmp_path / "kitchen.yaml"
    target.write_bytes(b"original")

    calls = {"n": 0}

    def _refuse(src: object, dst: object, **kwargs: object) -> None:
        calls["n"] += 1
        raise FileExistsError(str(dst))

    monkeypatch.setattr("os.rename", _refuse)
    with pytest.raises(FileExistsError):
        atomic_write_exclusive(target, b"clobber")

    assert calls["n"] == 1  # no retry on an existing destination
    assert target.read_bytes() == b"original"
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX hardlink fallback")
def test_atomic_write_exclusive_falls_back_on_hardlink_less_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link-unsupported errno degrades to the exclusive-open write."""
    target = tmp_path / "kitchen.yaml"

    def _no_links(src: object, dst: object, **kwargs: object) -> None:
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr("esphome_device_builder.helpers.atomic_io.os.link", _no_links)
    atomic_write_exclusive(target, b"esphome:\n")

    assert target.read_bytes() == b"esphome:\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX hardlink fallback")
def test_atomic_write_exclusive_fallback_still_refuses_existing_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback keeps exclusivity: an existing target raises FileExistsError."""
    target = tmp_path / "kitchen.yaml"
    target.write_bytes(b"original")

    def _no_links(src: object, dst: object, **kwargs: object) -> None:
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr("esphome_device_builder.helpers.atomic_io.os.link", _no_links)
    with pytest.raises(FileExistsError):
        atomic_write_exclusive(target, b"clobber")

    assert target.read_bytes() == b"original"
    assert list(tmp_path.glob("*.tmp")) == []
