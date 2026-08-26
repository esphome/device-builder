"""Tests for helpers.orphan_reaper."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

import pytest

from esphome_device_builder.helpers import orphan_reaper

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="/proc and fork semantics")


def _fork_exiting_child() -> int:
    """Fork a child from the main thread that exits immediately; return its pid, unreaped."""
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    return pid


def _wait_until_zombie(pid: int) -> None:
    """Poll until *pid* shows up as a zombie child of this process."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if pid in orphan_reaper._zombie_children(os.getpid()):
            return
        time.sleep(0.01)
    raise AssertionError("child never became a zombie")


def _spawn_zombie() -> int:
    """Fork a child that exits immediately; return its pid once zombied, unreaped."""
    pid = _fork_exiting_child()
    _wait_until_zombie(pid)
    return pid


def test_reaps_zombie_on_second_scan() -> None:
    """A zombie is detected on the first scan and waited on the second."""
    pid = _spawn_zombie()
    me = os.getpid()

    pending = orphan_reaper._reap_once(me, set(), set())
    assert pid in pending
    assert pid in orphan_reaper._zombie_children(me)

    orphan_reaper._reap_once(me, {pid}, set())
    assert pid not in orphan_reaper._zombie_children(me)
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)


def test_live_child_is_never_reaped() -> None:
    """A running child is not a zombie and stays untouched."""
    with subprocess.Popen(["/bin/sleep", "30"]) as proc:
        me = os.getpid()
        try:
            pending = orphan_reaper._reap_once(me, set(), set())
            assert proc.pid not in pending
            orphan_reaper._reap_once(me, {proc.pid}, set())
            assert proc.poll() is None
        finally:
            proc.kill()


def test_excluded_zombie_is_never_reaped() -> None:
    """A pid in the exclusion set stays unreaped across scans."""
    pid = _spawn_zombie()
    me = os.getpid()

    pending = orphan_reaper._reap_once(me, set(), {pid})
    assert pid not in pending
    orphan_reaper._reap_once(me, {pid}, {pid})
    assert pid in orphan_reaper._zombie_children(me)

    os.waitpid(pid, 0)


def test_zombie_children_missing_proc_entry() -> None:
    """A pid with no /proc children file yields an empty set."""
    assert orphan_reaper._zombie_children(2**22 + 12345) == set()


def test_zombie_children_child_vanished_before_stat(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child listed but gone by the stat read is skipped."""

    class _FakePath:
        def __init__(self, path: str) -> None:
            self._path = path

        def read_text(self, encoding: str) -> str:
            if self._path.endswith("/children"):
                return "4194321"
            raise FileNotFoundError(self._path)

    monkeypatch.setattr(orphan_reaper, "Path", _FakePath)
    assert orphan_reaper._zombie_children(os.getpid()) == set()


def test_reap_once_tolerates_waitpid_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pid that was already reaped elsewhere is skipped silently."""
    fake = 2**22 + 54321
    monkeypatch.setattr(orphan_reaper, "_zombie_children", lambda pid, exclude: {fake})
    assert orphan_reaper._reap_once(os.getpid(), {fake}, set()) == set()


def test_reap_once_logs_unexpected_waitpid_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A waitpid failure other than already-reaped is skipped and logged."""
    fake = 2**22 + 54321
    monkeypatch.setattr(orphan_reaper, "_zombie_children", lambda pid, exclude: {fake})

    def _raise(pid: int, options: int) -> tuple[int, int]:
        raise PermissionError(pid)

    monkeypatch.setattr(orphan_reaper.os, "waitpid", _raise)
    assert orphan_reaper._reap_once(os.getpid(), {fake}, set()) == set()


def test_reap_once_skips_log_when_nothing_was_reaped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``(0, 0)`` waitpid result claims no reap."""
    fake = 2**22 + 54321
    monkeypatch.setattr(orphan_reaper, "_zombie_children", lambda pid, exclude: {fake})
    monkeypatch.setattr(orphan_reaper.os, "waitpid", lambda pid, options: (0, 0))
    assert orphan_reaper._reap_once(os.getpid(), {fake}, set()) == set()


async def test_prepare_probes_children_listing() -> None:
    """The loop stays enabled when the /proc children listing is readable."""
    assert await orphan_reaper.OrphanReaperLoop()._prepare() is True


async def test_prepare_disables_loudly_without_children_listing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An unreadable children listing disables the loop with a warning."""

    class _UnreadablePath:
        def __init__(self, path: str) -> None:
            pass

        def read_text(self, encoding: str) -> str:
            raise PermissionError(encoding)

    monkeypatch.setattr(orphan_reaper, "Path", _UnreadablePath)
    assert await orphan_reaper.OrphanReaperLoop()._prepare() is False
    assert "orphan reaping disabled" in caplog.text


def test_should_reap_orphans_requires_pid1(monkeypatch: pytest.MonkeyPatch) -> None:
    """True only when running as PID 1."""
    monkeypatch.setattr(orphan_reaper.os, "getpid", lambda: 1)
    assert orphan_reaper.should_reap_orphans()
    monkeypatch.setattr(orphan_reaper.os, "getpid", lambda: 4242)
    assert not orphan_reaper.should_reap_orphans()


async def test_reaper_loop_work_carries_pending() -> None:
    """One tick records the zombie as pending; the next tick reaps it."""
    reaper = orphan_reaper.OrphanReaperLoop()
    pid = _fork_exiting_child()
    await asyncio.to_thread(_wait_until_zombie, pid)

    await reaper._work()
    assert pid in reaper._pending

    await reaper._work()
    assert pid not in reaper._pending
    assert pid not in await asyncio.to_thread(orphan_reaper._zombie_children, os.getpid())
