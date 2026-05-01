"""Stop-button cancellation on Windows uses ``taskkill /F /T``.

The POSIX path (``test_firmware_stop.py``) relies on process groups
and ``killpg`` — primitives that don't exist on Windows. This module
covers the Windows-specific branch in ``_terminate_current_process``:
``taskkill`` walks the kernel's parent-PID tree and force-kills the
whole subtree in one shot.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock

import pytest

from esphome_device_builder.controllers.firmware import (
    FirmwareController,
    _terminate_subtree_windows,
)
from esphome_device_builder.helpers.subprocess import create_subprocess_exec

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows-only termination path; POSIX is covered in test_firmware_stop.py.",
)


@pytest.fixture
def controller() -> FirmwareController:
    """Stand up a FirmwareController shell — only the bits termination touches."""
    ctrl = FirmwareController.__new__(FirmwareController)
    ctrl._current_process = None  # type: ignore[attr-defined]
    ctrl._current_job = MagicMock(job_id="test-job")  # type: ignore[attr-defined]
    return ctrl


async def test_terminate_kills_subprocess_via_taskkill(
    controller: FirmwareController,
) -> None:
    """The Windows stop path force-kills the running subprocess via taskkill /F /T."""
    proc = await create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    controller._current_process = proc  # type: ignore[attr-defined]

    try:
        await controller._terminate_current_process()
        # taskkill /F /T schedules termination synchronously; the
        # subprocess should exit within seconds.
        await asyncio.wait_for(proc.wait(), timeout=5.0)
        assert proc.returncode is not None
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def test_terminate_subtree_windows_returns_false_when_taskkill_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing ``taskkill`` is logged and reported, not raised."""

    async def _missing(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _missing)
    assert await _terminate_subtree_windows(12345) is False
