"""Reap orphaned child processes when running as PID 1."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Set as AbstractSet
from pathlib import Path

from .async_ import run_in_executor
from .presence_gated_loop import PresenceGatedLoop
from .subprocess import live_child_pids

_LOGGER = logging.getLogger(__name__)

_SCAN_INTERVAL_SECONDS = 30.0


def should_reap_orphans() -> bool:
    """Whether this process must reap orphans itself (Linux PID 1, no init in front)."""
    return sys.platform == "linux" and os.getpid() == 1


class OrphanReaperLoop(PresenceGatedLoop[None]):
    """Periodically wait on zombie children this process never spawned."""

    _label = "orphan reaper"
    _bootstrap_delay = _SCAN_INTERVAL_SECONDS
    _interval = _SCAN_INTERVAL_SECONDS

    def __init__(self) -> None:
        # presence=None runs the loop ungated: orphans accrue whether
        # or not a dashboard client is connected.
        super().__init__(None)
        self._pid = os.getpid()
        self._pending: set[int] = set()

    async def _prepare(self) -> bool:
        """Probe the /proc children listing; an unreadable one disables the loop loudly."""
        if await run_in_executor(_children_listing_readable, self._pid):
            return True
        _LOGGER.warning(
            "Cannot read /proc/%d/task/%d/children; orphan reaping disabled",
            self._pid,
            self._pid,
        )
        return False

    async def _work(self) -> None:
        self._pending = await run_in_executor(
            _reap_once, self._pid, self._pending, live_child_pids()
        )


def _reap_once(pid: int, pending: set[int], exclude: AbstractSet[int]) -> set[int]:
    """
    Wait on *pid*'s zombie children seen in *pending*; return this scan's zombies.

    Only a pid in zombie state on two consecutive scans is reaped, and
    a pid in *exclude* is never touched.
    """
    zombies = _zombie_children(pid, exclude)
    for child in zombies & pending:
        try:
            reaped, _ = os.waitpid(child, os.WNOHANG)
        except ChildProcessError:
            continue
        except OSError as err:
            _LOGGER.debug("waitpid(%d) failed: %s", child, err)
            continue
        if reaped:
            _LOGGER.debug("Reaped orphaned process %d", child)
    return zombies - pending


def _children_listing_readable(pid: int) -> bool:
    """Whether the /proc children listing the scans depend on can be read."""
    try:
        _children_path(pid).read_text(encoding="ascii")
    except OSError:
        return False
    return True


def _zombie_children(pid: int, exclude: AbstractSet[int] = frozenset()) -> set[int]:
    """Return *pid*'s direct children in zombie state, skipping *exclude* unread."""
    try:
        children = _children_path(pid).read_text(encoding="ascii").split()
    except OSError:
        return set()
    zombies: set[int] = set()
    for token in children:
        child = int(token)
        if child in exclude:
            continue
        try:
            stat = Path(f"/proc/{child}/stat").read_text(encoding="ascii")
        except OSError:
            continue
        # comm (field 2) may contain spaces/parens; the state field is
        # the first token after the closing paren.
        if stat.rpartition(")")[2].split()[0] == "Z":
            zombies.add(child)
    return zombies


def _children_path(pid: int) -> Path:
    """Path of the kernel's per-task children listing for *pid*'s main thread."""
    return Path(f"/proc/{pid}/task/{pid}/children")
