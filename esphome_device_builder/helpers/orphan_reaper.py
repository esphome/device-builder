"""Reap orphaned child processes when running as PID 1.

In the plain Docker image the dashboard is PID 1 with no init in front,
so every orphaned process (git's self-detached auto-maintenance, the
survivors of a killed compile tree) is reparented to us, and nothing
ever waits on it — asyncio's child watcher and ``subprocess.run`` only
reap pids they spawned themselves — leaving it ``<defunct>`` forever
(issue #2635).
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import sys

from .async_ import create_eager_task
from .subprocess import live_child_pids

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = 30.0


def maybe_start_orphan_reaper() -> asyncio.Task[None] | None:
    """Start the reaper loop when this process must reap orphans itself (Linux PID 1)."""
    if sys.platform != "linux" or os.getpid() != 1:
        return None
    return create_eager_task(run_reaper_loop())


async def run_reaper_loop() -> None:
    """Periodically reap confirmed-zombie children; runs until cancelled."""
    loop = asyncio.get_running_loop()
    pid = os.getpid()
    pending: set[int] = set()
    while True:
        await asyncio.sleep(SCAN_INTERVAL)
        pending = await loop.run_in_executor(None, reap_once, pid, pending, live_child_pids())


def reap_once(pid: int, pending: set[int], exclude: set[int]) -> set[int]:
    """
    Wait on *pid*'s zombie children present in *pending*; return this scan's zombies.

    A pid in *exclude* (an asyncio-spawned child the loop still owns) is
    never touched. Beyond that, only a pid in zombie state on two
    consecutive scans is reaped: an in-process spawn is collected by its
    own waiter within milliseconds of exiting, so it can't survive a
    scan interval — the gate keeps a pid whose owner still needs its
    return code from being stolen.
    """
    zombies = _zombie_children(pid) - exclude
    for child in zombies & pending:
        try:
            os.waitpid(child, os.WNOHANG)
        except (ChildProcessError, OSError):
            continue
        _LOGGER.debug("Reaped orphaned process %d", child)
    return zombies - pending


def _zombie_children(pid: int) -> set[int]:
    """Return *pid*'s direct children currently in zombie state."""
    try:
        children = _read_proc(f"/proc/{pid}/task/{pid}/children").split()
    except OSError:
        return set()
    zombies: set[int] = set()
    for child in children:
        try:
            stat = _read_proc(f"/proc/{child}/stat")
        except OSError:
            continue
        # comm (field 2) may contain spaces/parens; the state field is
        # the first token after the closing paren.
        if stat.rpartition(")")[2].split()[0] == "Z":
            zombies.add(int(child))
    return zombies


def _read_proc(path: str) -> str:
    """Read a small /proc file as text."""
    with pathlib.Path(path).open(encoding="ascii") as f:
        return f.read()
