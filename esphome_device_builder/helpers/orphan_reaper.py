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
        super().__init__(None)
        self._pid = os.getpid()
        self._pending: set[int] = set()

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
            os.waitpid(child, os.WNOHANG)
        except OSError:
            continue
        _LOGGER.debug("Reaped orphaned process %d", child)
    return zombies - pending


def _zombie_children(pid: int, exclude: AbstractSet[int] = frozenset()) -> set[int]:
    """Return *pid*'s direct children in zombie state, skipping *exclude* unread."""
    try:
        children = Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="ascii").split()
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
