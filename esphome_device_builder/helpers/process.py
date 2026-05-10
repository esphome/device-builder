"""
Subprocess teardown helpers — platform-aware kill + grace-period orchestration.

The dashboard spawns subprocesses in a few different shapes:

* **Compile / upload / install jobs** spawn ``esphome``, which forks
  PlatformIO, which forks ``gcc`` / ``esptool`` / ``mklittlefs``. The
  whole tree has to come down on cancel.
* **YAML validator session** (``editor.py``) spawns a single
  ``esphome vscode`` subprocess. Single process, no fork chain.
* **Device log streams** (``devices/controller.py``) spawn ``esphome
  logs`` for live tailing. Single process.
* **Startup probes** (``firmware/helpers._verify_esphome_importable``)
  spawn ``esphome --version`` with a hard timeout.

Three patterns end up open-coded in the call sites without this
module:

* ``with suppress(ProcessLookupError): proc.kill()`` for the race
  where the child exits between the ``returncode is None`` check and
  the kill — this is ``kill_quietly`` here.
* ``os.killpg(os.getpgid(pid), sig)`` with ``ProcessLookupError`` /
  ``PermissionError`` handling — POSIX subtree signal, exposed as
  ``_signal_process_group``.
* Windows ``taskkill /F /T`` with timeout + retcode handling —
  exposed as ``_terminate_subtree_windows``.

The orchestration helper ``terminate_subtree_with_grace`` ties those
together: SIGTERM the group → wait the grace window → SIGKILL the
group on POSIX; ``taskkill`` with a single-shot kill fallback on
Windows. That's the shape ``FirmwareController._terminate_current_process``
needs.

POSIX vs Windows asymmetry is deliberate: the POSIX path has a
graceful SIGTERM stage because well-behaved tools (``esptool``,
``platformio``) honour it and clean up serial ports / partial
writes. The Windows compile chain ignores ``CTRL_BREAK_EVENT`` and
``WM_CLOSE`` — there's no point sending a polite signal nobody's
listening for, so we go straight to ``taskkill``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from .subprocess import create_subprocess_exec, kill_quietly

__all__ = [
    "kill_quietly",
    "terminate_subtree_with_grace",
]

_LOGGER = logging.getLogger(__name__)

# Window between SIGTERM and SIGKILL on POSIX, and timeout on the
# ``taskkill`` invocation on Windows. Tuned for the compile chain
# (esphome → platformio → gcc / esptool): three seconds is plenty for
# esptool to release the serial port and platformio to flush its log,
# and short enough that a hung compiler doesn't make the user wait.
_TERMINATE_GRACE_SECONDS = 3.0


def _signal_process_group(pid: int, sig: int) -> bool:
    """
    Send *sig* to the process group of *pid*; return True iff delivered.

    Used to take down the whole esphome → platformio → gcc tree when
    the user hits Stop. ``proc.terminate()`` / ``proc.kill()`` only
    signal the direct child (the python esphome process), so the
    compiler grandchildren keep running and the build effectively
    ignores the cancel. Pair this with ``start_new_session=True`` at
    the spawn site: that makes the spawned process the leader of a
    new session (and a new process group), and its descendants
    inherit that group. The dashboard process itself is *not* in the
    same group — ``killpg(getpgid(spawned_pid), sig)`` therefore
    targets the build subtree without touching us.

    POSIX-only — ``os.getpgid`` / ``os.killpg`` don't exist on Windows.
    The Windows path goes through ``_terminate_subtree_windows`` instead.

    Falls back gracefully:

    * ``ProcessLookupError`` — the process already exited; nothing to do.
    * ``PermissionError`` — we lost the right to signal it; treat as a
      no-op rather than crashing the controller.
    """
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return False
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return False
    except PermissionError:
        _LOGGER.warning("Permission denied signalling pgid %d (sig %s)", pgid, sig)
        return False
    return True


async def _terminate_subtree_windows(pid: int) -> bool:
    """
    Forcibly kill *pid* and its descendants on Windows; return True iff successful.

    Windows has no process groups in the POSIX sense, so we shell out to
    ``taskkill /F /T /PID`` — ``/T`` walks the parent-child tree from
    *pid* down, ``/F`` is the forceful equivalent of SIGKILL. There's no
    useful "polite" stage here: a compile chain (esphome → platformio →
    gcc / esptool) ignores ``WM_CLOSE`` / ``CTRL_BREAK_EVENT`` anyway,
    so we go straight to the kill.

    Returns False (and logs a warning) when ``taskkill`` is missing,
    times out, or exits non-zero (access denied, invalid pid, partial
    failure). The caller should fall back to ``proc.kill()`` so the
    parent at least dies even when the tree-walk fails.
    """
    try:
        killer = await create_subprocess_exec(
            "taskkill",
            "/F",
            "/T",
            "/PID",
            str(pid),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        _LOGGER.warning("taskkill not found on PATH — can't tree-kill pid %d", pid)
        return False
    try:
        await asyncio.wait_for(killer.wait(), timeout=_TERMINATE_GRACE_SECONDS)
    except TimeoutError:
        _LOGGER.warning("taskkill timed out for pid %d", pid)
        kill_quietly(killer)
        return False
    if killer.returncode != 0:
        _LOGGER.warning(
            "taskkill exited %s for pid %d — caller should fall back to proc.kill()",
            killer.returncode,
            pid,
        )
        return False
    return True


async def terminate_subtree_with_grace(
    proc: asyncio.subprocess.Process,
    *,
    grace_seconds: float = _TERMINATE_GRACE_SECONDS,
    job_label: str = "subprocess",
) -> None:
    """
    Bring down *proc* and its descendants, gracefully if possible.

    POSIX: SIGTERM the process group, wait *grace_seconds* for the
    tree to exit on its own, then SIGKILL the group. Requires the
    spawn site to have used ``start_new_session=True`` so a process
    group exists to signal — without that, only the direct child
    receives the signal and the compiler grandchildren orphan.

    Windows: ``taskkill /F /T`` to walk the kernel's parent-child
    accounting and force-kill the subtree. There's no graceful
    stage on Windows because the compile chain ignores polite
    signals; if ``taskkill`` is missing or hangs, fall back to
    ``proc.kill()`` so the direct child at least dies and the
    runner loop can finalise the job.

    No-op when *proc* has already exited. *job_label* is used in
    the warning log if the SIGTERM grace window expires (POSIX only).
    """
    if proc.returncode is not None:
        return
    if sys.platform == "win32":
        if not await _terminate_subtree_windows(proc.pid):
            kill_quietly(proc)
        return
    if not _signal_process_group(proc.pid, signal.SIGTERM):
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
    except TimeoutError:
        _LOGGER.warning(
            "Subprocess for %s ignored SIGTERM after %.1fs — sending SIGKILL",
            job_label,
            grace_seconds,
        )
        _signal_process_group(proc.pid, signal.SIGKILL)
