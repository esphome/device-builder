"""Kill-on-close Win32 job objects — the Windows process-tree kill primitive.

Importable on every platform; the pywin32 bindings exist only on Windows.
"""

from __future__ import annotations

import logging
import sys
from contextlib import suppress
from typing import Any

try:
    import pywintypes
    import win32api
    import win32con
    import win32job
except ImportError:  # non-Windows
    pywintypes = win32api = win32con = win32job = None

__all__ = ["WindowsJobObject"]

_LOGGER = logging.getLogger(__name__)


class WindowsJobObject:
    """Owns a kill-on-close Win32 job-object handle wrapping one spawned process tree."""

    def __init__(self, handle: Any) -> None:
        self._handle: Any | None = handle

    @classmethod
    def create_for_pid(cls, pid: int) -> WindowsJobObject | None:
        """Create a kill-on-close job object and assign *pid*'s tree to it; None on failure."""
        if win32job is None:
            if sys.platform == "win32":
                _LOGGER.warning("pywin32 is unavailable; cancel falls back to the taskkill sweep")
            return None
        job = None
        try:
            job = win32job.CreateJobObject(None, "")
            info = win32job.QueryInformationJobObject(
                job, win32job.JobObjectExtendedLimitInformation
            )
            info["BasicLimitInformation"]["LimitFlags"] |= (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
            # No PID-reuse race here: the asyncio Process keeps its child
            # handle open until reaped, which pins the pid.
            proc = win32api.OpenProcess(
                win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, 0, pid
            )
            try:
                win32job.AssignProcessToJobObject(job, proc)
            finally:
                proc.Close()
        except pywintypes.error as err:
            _LOGGER.warning("Job-object setup failed for pid %d: %s", pid, err)
            if job is not None:
                with suppress(pywintypes.error):
                    job.Close()
            return None
        return cls(job)

    def terminate(self) -> bool:
        """Force-kill every process in the job; True iff the kernel accepted the kill."""
        if self._handle is None:
            return False
        try:
            win32job.TerminateJobObject(self._handle, 1)
        except pywintypes.error as err:
            _LOGGER.warning("TerminateJobObject failed: %s", err)
            return False
        return True

    def close(self) -> None:
        """Release the handle (kill-on-close reaps any survivors); idempotent."""
        if self._handle is None:
            return
        handle, self._handle = self._handle, None
        try:
            handle.Close()
        except pywintypes.error as err:
            _LOGGER.warning("Closing job object failed (tree may leak): %s", err)
