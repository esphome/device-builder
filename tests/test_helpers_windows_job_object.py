"""Tests for ``helpers/windows_job_object.py`` — the Win32 tree-kill primitive.

Cross-platform: the cached ``_kernel32`` accessor is monkeypatched with
a recording fake, so the create → set-limits → assign call sequence and
every failure branch run on any OS in the matrix. Real-kernel coverage
lives in the windows-only integration test in
``tests/controllers/firmware/test_stop_windows.py``.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass, field
from typing import Any

import pytest

from esphome_device_builder.helpers import windows_job_object as wjo_module
from esphome_device_builder.helpers.windows_job_object import (
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    _PROCESS_SET_QUOTA,
    _PROCESS_TERMINATE,
    WindowsJobObject,
)

_JOB_HANDLE = 0x1111
_PROC_HANDLE = 0x2222


@dataclass
class _FakeKernel32:
    """Recording kernel32 stand-in with tunable per-call return values."""

    create_job_result: int | None = _JOB_HANDLE
    set_information_result: int = 1
    open_process_result: int | None = _PROC_HANDLE
    assign_result: int = 1
    terminate_result: int = 1
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)
    closed_handles: list[int] = field(default_factory=list)

    def CreateJobObjectW(self, *args: Any) -> int | None:  # noqa: N802
        self.calls.append(("CreateJobObjectW", args))
        return self.create_job_result

    def SetInformationJobObject(self, *args: Any) -> int:  # noqa: N802
        self.calls.append(("SetInformationJobObject", args))
        return self.set_information_result

    def OpenProcess(self, *args: Any) -> int | None:  # noqa: N802
        self.calls.append(("OpenProcess", args))
        return self.open_process_result

    def AssignProcessToJobObject(self, *args: Any) -> int:  # noqa: N802
        self.calls.append(("AssignProcessToJobObject", args))
        return self.assign_result

    def TerminateJobObject(self, *args: Any) -> int:  # noqa: N802
        self.calls.append(("TerminateJobObject", args))
        return self.terminate_result

    def CloseHandle(self, handle: int) -> int:  # noqa: N802
        self.calls.append(("CloseHandle", (handle,)))
        self.closed_handles.append(handle)
        return 1

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


@pytest.fixture
def fake_kernel32(monkeypatch: pytest.MonkeyPatch) -> _FakeKernel32:
    """Patch the cached ``_kernel32`` accessor with a recording fake."""
    fake = _FakeKernel32()
    monkeypatch.setattr(wjo_module, "_kernel32", lambda: fake)
    return fake


def test_create_for_pid_happy_path(fake_kernel32: _FakeKernel32) -> None:
    """Create → kill-on-close limit → open pid → assign → close the process handle."""
    job = WindowsJobObject.create_for_pid(4242)

    assert job is not None
    assert fake_kernel32.names() == [
        "CreateJobObjectW",
        "SetInformationJobObject",
        "OpenProcess",
        "AssignProcessToJobObject",
        "CloseHandle",
    ]
    set_args = fake_kernel32.calls[1][1]
    assert set_args[0] == _JOB_HANDLE
    assert set_args[1] == _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS
    info = ctypes.cast(set_args[2], ctypes.POINTER(wjo_module._JobObjectExtendedLimitInformation))
    assert info.contents.BasicLimitInformation.LimitFlags == _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    assert fake_kernel32.calls[2][1] == (_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, 0, 4242)
    assert fake_kernel32.calls[3][1] == (_JOB_HANDLE, _PROC_HANDLE)
    # Only the short-lived process handle is released; the job handle stays open.
    assert fake_kernel32.closed_handles == [_PROC_HANDLE]


def test_create_for_pid_none_when_create_fails(fake_kernel32: _FakeKernel32) -> None:
    """A NULL job handle short-circuits with nothing to close."""
    fake_kernel32.create_job_result = None
    assert WindowsJobObject.create_for_pid(4242) is None
    assert fake_kernel32.closed_handles == []


def test_create_for_pid_none_when_set_information_fails(fake_kernel32: _FakeKernel32) -> None:
    """A failed limit set closes the job handle and returns None."""
    fake_kernel32.set_information_result = 0
    assert WindowsJobObject.create_for_pid(4242) is None
    assert fake_kernel32.closed_handles == [_JOB_HANDLE]


def test_create_for_pid_none_when_open_process_fails(fake_kernel32: _FakeKernel32) -> None:
    """An unopenable pid closes the job handle and returns None."""
    fake_kernel32.open_process_result = None
    assert WindowsJobObject.create_for_pid(4242) is None
    assert fake_kernel32.closed_handles == [_JOB_HANDLE]


def test_create_for_pid_none_when_assign_fails(fake_kernel32: _FakeKernel32) -> None:
    """A failed assignment closes both handles and returns None."""
    fake_kernel32.assign_result = 0
    assert WindowsJobObject.create_for_pid(4242) is None
    assert fake_kernel32.closed_handles == [_PROC_HANDLE, _JOB_HANDLE]


def test_terminate_maps_kernel_result(fake_kernel32: _FakeKernel32) -> None:
    """``terminate`` reports the kernel verdict and targets the held handle."""
    job = WindowsJobObject(_JOB_HANDLE)
    assert job.terminate() is True
    assert fake_kernel32.calls[-1] == ("TerminateJobObject", (_JOB_HANDLE, 1))
    fake_kernel32.terminate_result = 0
    assert job.terminate() is False


def test_terminate_false_after_close(fake_kernel32: _FakeKernel32) -> None:
    """A closed wrapper refuses to terminate (no handle to target)."""
    job = WindowsJobObject(_JOB_HANDLE)
    job.close()
    assert job.terminate() is False
    assert "TerminateJobObject" not in fake_kernel32.names()


def test_close_is_idempotent(fake_kernel32: _FakeKernel32) -> None:
    """Double-close releases the handle exactly once."""
    job = WindowsJobObject(_JOB_HANDLE)
    job.close()
    job.close()
    assert fake_kernel32.closed_handles == [_JOB_HANDLE]
