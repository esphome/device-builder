"""Tests for helpers.orphan_reaper."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from esphome_device_builder.helpers import orphan_reaper

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="/proc and fork semantics")


def _spawn_zombie() -> int:
    """Fork a child that exits immediately; return its pid, unreaped."""
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if pid in orphan_reaper._zombie_children(os.getpid()):
            return pid
        time.sleep(0.01)
    raise AssertionError("child never became a zombie")


def test_reaps_zombie_on_second_scan() -> None:
    """A zombie is detected on the first scan and waited on the second."""
    pid = _spawn_zombie()
    me = os.getpid()

    pending = orphan_reaper._reap_once(me, set(), set())
    assert pid in pending
    assert pid in orphan_reaper._zombie_children(me)

    orphan_reaper._reap_once(me, pending, set())
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
