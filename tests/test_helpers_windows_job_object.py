"""Tests for ``helpers/windows_job_object.py`` — the Win32 tree-kill primitive.

Cross-platform: the module's pywin32 bindings are monkeypatched with
recording fakes, so the create → set-limits → assign sequence and every
failure branch run on any OS in the matrix. Real-kernel coverage lives
in the windows-only integration test in
``tests/controllers/firmware/test_stop_windows.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from esphome_device_builder.helpers import windows_job_object as wjo_module
from esphome_device_builder.helpers.windows_job_object import (
    _PROCESS_SET_QUOTA,
    _PROCESS_TERMINATE,
    WindowsJobObject,
)

_KILL_ON_JOB_CLOSE = 0x2000
_EXTENDED_INFO_CLASS = 9


class _Win32Error(Exception):
    """Stands in for ``pywintypes.error``."""


class _FakeHandle:
    """PyHANDLE stand-in: records closes, idempotent like the real thing."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.close_calls = 0

    def Close(self) -> None:  # noqa: N802
        self.close_calls += 1


@dataclass
class _FakeWin32:
    """Recording ``win32job`` + ``win32api`` stand-in with failure toggles."""

    fail_at: str | None = None
    terminate_error: bool = False
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)
    job: _FakeHandle = field(default_factory=lambda: _FakeHandle("job"))
    proc: _FakeHandle = field(default_factory=lambda: _FakeHandle("proc"))
    info: dict[str, Any] = field(
        default_factory=lambda: {"BasicLimitInformation": {"LimitFlags": 0}}
    )

    def _record(self, name: str, args: tuple[Any, ...]) -> None:
        self.calls.append((name, args))
        if self.fail_at == name:
            raise _Win32Error(5, name, "denied")

    def CreateJobObject(self, *args: Any) -> _FakeHandle:  # noqa: N802
        self._record("CreateJobObject", args)
        return self.job

    def QueryInformationJobObject(self, *args: Any) -> dict[str, Any]:  # noqa: N802
        self._record("QueryInformationJobObject", args)
        return self.info

    def SetInformationJobObject(self, *args: Any) -> None:  # noqa: N802
        self._record("SetInformationJobObject", args)

    def OpenProcess(self, *args: Any) -> _FakeHandle:  # noqa: N802
        self._record("OpenProcess", args)
        return self.proc

    def AssignProcessToJobObject(self, *args: Any) -> None:  # noqa: N802
        self._record("AssignProcessToJobObject", args)

    def TerminateJobObject(self, *args: Any) -> None:  # noqa: N802
        self.calls.append(("TerminateJobObject", args))
        if self.terminate_error:
            raise _Win32Error(6, "TerminateJobObject", "invalid handle")

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


@pytest.fixture
def fake_win32(monkeypatch: pytest.MonkeyPatch) -> _FakeWin32:
    """Patch the module's pywin32 bindings with a recording fake."""
    fake = _FakeWin32()
    fake.JobObjectExtendedLimitInformation = _EXTENDED_INFO_CLASS  # type: ignore[attr-defined]
    fake.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = _KILL_ON_JOB_CLOSE  # type: ignore[attr-defined]
    monkeypatch.setattr(wjo_module, "win32job", fake)
    monkeypatch.setattr(wjo_module, "win32api", fake)
    monkeypatch.setattr(wjo_module, "pywintypes", SimpleNamespace(error=_Win32Error))
    return fake


def test_create_for_pid_happy_path(fake_win32: _FakeWin32) -> None:
    """Create → kill-on-close limit → open pid → assign → close the process handle."""
    job = WindowsJobObject.create_for_pid(4242)

    assert job is not None
    assert fake_win32.names() == [
        "CreateJobObject",
        "QueryInformationJobObject",
        "SetInformationJobObject",
        "OpenProcess",
        "AssignProcessToJobObject",
    ]
    set_args = fake_win32.calls[2][1]
    assert set_args == (fake_win32.job, _EXTENDED_INFO_CLASS, fake_win32.info)
    assert fake_win32.info["BasicLimitInformation"]["LimitFlags"] & _KILL_ON_JOB_CLOSE
    assert fake_win32.calls[3][1] == (_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, 0, 4242)
    assert fake_win32.calls[4][1] == (fake_win32.job, fake_win32.proc)
    # Only the short-lived process handle is released; the job handle stays open.
    assert fake_win32.proc.close_calls == 1
    assert fake_win32.job.close_calls == 0


@pytest.mark.parametrize(
    "fail_at",
    ["CreateJobObject", "SetInformationJobObject", "OpenProcess", "AssignProcessToJobObject"],
)
def test_create_for_pid_none_on_failure(fake_win32: _FakeWin32, fail_at: str) -> None:
    """A win32 error at any step logs and returns None instead of raising."""
    fake_win32.fail_at = fail_at
    assert WindowsJobObject.create_for_pid(4242) is None


def test_create_for_pid_assign_failure_still_closes_process_handle(
    fake_win32: _FakeWin32,
) -> None:
    """The process handle is released even when assignment raises."""
    fake_win32.fail_at = "AssignProcessToJobObject"
    assert WindowsJobObject.create_for_pid(4242) is None
    assert fake_win32.proc.close_calls == 1


def test_terminate_maps_kernel_result(fake_win32: _FakeWin32) -> None:
    """``terminate`` reports the kernel verdict and targets the held handle."""
    job = WindowsJobObject(fake_win32.job)
    assert job.terminate() is True
    assert fake_win32.calls[-1] == ("TerminateJobObject", (fake_win32.job, 1))
    fake_win32.terminate_error = True
    assert job.terminate() is False


def test_terminate_false_after_close(fake_win32: _FakeWin32) -> None:
    """A closed wrapper refuses to terminate (no handle to target)."""
    job = WindowsJobObject(fake_win32.job)
    job.close()
    assert job.terminate() is False
    assert "TerminateJobObject" not in fake_win32.names()


def test_close_is_idempotent(fake_win32: _FakeWin32) -> None:
    """Double-close releases the handle exactly once."""
    job = WindowsJobObject(fake_win32.job)
    job.close()
    job.close()
    assert fake_win32.job.close_calls == 1
