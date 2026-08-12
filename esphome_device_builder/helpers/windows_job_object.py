"""Win32 job-object wrapper — the Windows process-tree kill primitive.

A kill-on-close job object is the Windows equivalent of the POSIX
process group: ``TerminateJobObject`` takes down the whole spawned
tree, and closing the handle reaps survivors. Importable on every
platform; kernel32 loads lazily.
"""

from __future__ import annotations

import ctypes
import logging
from functools import cache
from typing import Any

__all__ = ["WindowsJobObject"]

_LOGGER = logging.getLogger(__name__)

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _last_error() -> int:
    """``ctypes.get_last_error()`` where it exists, else 0."""
    get = getattr(ctypes, "get_last_error", None)
    return get() if get is not None else 0


@cache
def _kernel32() -> Any:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    # Explicit prototypes — the ctypes default return type is c_int,
    # which truncates 64-bit HANDLE values.
    k32.CreateJobObjectW.restype = ctypes.c_void_p
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    k32.SetInformationJobObject.restype = ctypes.c_int
    k32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k32.AssignProcessToJobObject.restype = ctypes.c_int
    k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    k32.TerminateJobObject.restype = ctypes.c_int
    k32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k32.CloseHandle.restype = ctypes.c_int
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    return k32


class WindowsJobObject:
    """Owns a kill-on-close Win32 job-object handle wrapping one spawned process tree."""

    def __init__(self, handle: int) -> None:
        self._handle: int | None = handle

    @classmethod
    def create_for_pid(cls, pid: int) -> WindowsJobObject | None:
        """Create a kill-on-close job object and assign *pid*'s tree to it; None on failure."""
        k32 = _kernel32()
        job = k32.CreateJobObjectW(None, None)
        if not job:
            _LOGGER.warning("CreateJobObjectW failed (error %d)", _last_error())
            return None
        info = _JobObjectExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            _LOGGER.warning("SetInformationJobObject failed (error %d)", _last_error())
            k32.CloseHandle(job)
            return None
        # No PID-reuse race here: the asyncio Process keeps its child
        # handle open until reaped, which pins the pid.
        proc = k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, 0, pid)
        if not proc:
            _LOGGER.warning("OpenProcess failed for pid %d (error %d)", pid, _last_error())
            k32.CloseHandle(job)
            return None
        assigned = k32.AssignProcessToJobObject(job, proc)
        error = _last_error()
        k32.CloseHandle(proc)
        if not assigned:
            _LOGGER.warning("AssignProcessToJobObject failed for pid %d (error %d)", pid, error)
            k32.CloseHandle(job)
            return None
        return cls(job)

    def terminate(self) -> bool:
        """Force-kill every process in the job; True iff the kernel accepted the kill."""
        if self._handle is None:
            return False
        if not _kernel32().TerminateJobObject(self._handle, 1):
            _LOGGER.warning("TerminateJobObject failed (error %d)", _last_error())
            return False
        return True

    def close(self) -> None:
        """Release the handle (kill-on-close reaps any survivors); idempotent."""
        if self._handle is None:
            return
        _kernel32().CloseHandle(self._handle)
        self._handle = None
