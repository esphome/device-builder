"""Tests for ``helpers/process.py`` — subprocess teardown helpers.

The platform-specific halves (POSIX ``_signal_process_group`` and
Windows ``_terminate_subtree_windows``) are exercised by the
firmware-stop integration tests; this file covers the shared
helpers ``kill_quietly`` and ``terminate_subtree_with_grace`` that
sit on top of them.
"""

from __future__ import annotations

import signal
import sys
from dataclasses import dataclass, field
from typing import Any

import pytest

from esphome_device_builder.helpers.process import (
    kill_quietly,
    terminate_subtree_with_grace,
)

# Platform skipif markers — every signal-group test below is POSIX-only,
# every taskkill test is Windows-only. Naming them once keeps the per-test
# decorator a one-liner and the reason string in lockstep across the file.
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal-group path")
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows taskkill path")


@dataclass
class _FakeProc:
    """
    Minimal ``asyncio.subprocess.Process`` stand-in for these tests.

    Only the attributes / methods ``terminate_subtree_with_grace``
    and ``kill_quietly`` actually touch: ``pid``, ``returncode``,
    ``kill()``, ``wait()``. ``kill()`` records the call count so a
    test can assert "the fallback fired" without manual bookkeeping.
    """

    pid: int = 12345
    returncode: int | None = None
    kill_calls: int = 0

    def kill(self) -> None:
        self.kill_calls += 1

    async def wait(self) -> int:
        # The real ``Process.wait`` blocks until exit and sets
        # returncode; mirror that so callers checking
        # ``proc.returncode is not None`` after the await behave
        # the same.
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


@pytest.fixture
def fake_proc() -> _FakeProc:
    """Build a live stub-proc: ``pid`` set, ``returncode=None``, kill() counted."""
    return _FakeProc()


@dataclass
class _FakeSignalGroup:
    """Recorder + tunable return value for the patched ``_signal_process_group``.

    Tests assert on ``calls`` to verify which signals were sent in
    which order; setting ``return_value = False`` simulates the
    "process group already gone" branch without a separate fixture.
    """

    calls: list[tuple[int, int]] = field(default_factory=list)
    return_value: bool = True


@pytest.fixture
def fake_signal_group(monkeypatch: pytest.MonkeyPatch) -> _FakeSignalGroup:
    """Patch ``_signal_process_group`` with a recorder; return the handle."""
    fake = _FakeSignalGroup()

    def _impl(pid: int, sig: int) -> bool:
        fake.calls.append((pid, sig))
        return fake.return_value

    monkeypatch.setattr(
        "esphome_device_builder.helpers.process._signal_process_group",
        _impl,
    )
    return fake


# ---------------------------------------------------------------------------
# kill_quietly
# ---------------------------------------------------------------------------


def test_kill_quietly_swallows_process_lookup_error() -> None:
    """A ``ProcessLookupError`` from ``proc.kill()`` doesn't propagate.

    Race shape: caller checks ``proc.returncode is None``, the
    child exits, the kill then raises because the pid is reaped.
    The helper wraps the kill so call sites don't repeat the
    suppress block — drop the suppress and this test surfaces it.
    """

    class _DeadProc:
        def kill(self) -> None:
            raise ProcessLookupError

    kill_quietly(_DeadProc())  # type: ignore[arg-type]


def test_kill_quietly_calls_kill_when_alive(fake_proc: _FakeProc) -> None:
    """A live proc gets ``proc.kill()`` invoked exactly once."""
    kill_quietly(fake_proc)  # type: ignore[arg-type]
    assert fake_proc.kill_calls == 1


# ---------------------------------------------------------------------------
# terminate_subtree_with_grace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminate_subtree_with_grace_no_op_on_already_exited(
    fake_proc: _FakeProc,
) -> None:
    """Already-exited proc returns immediately without trying to signal."""
    fake_proc.returncode = 0
    await terminate_subtree_with_grace(fake_proc)  # type: ignore[arg-type]
    assert fake_proc.kill_calls == 0


@posix_only
@pytest.mark.asyncio
async def test_terminate_subtree_with_grace_sigterm_then_exit_no_kill(
    fake_proc: _FakeProc,
    fake_signal_group: _FakeSignalGroup,
) -> None:
    """SIGTERM is sent; child exits within grace → no SIGKILL escalation.

    Pin the happy POSIX path: process group gets a SIGTERM, the
    proc exits before the grace window closes, the SIGKILL branch
    is never reached. A regression that flipped the wait-for to
    raise (or skipped the wait entirely) would land in the
    SIGKILL branch and assert here.
    """
    await terminate_subtree_with_grace(fake_proc)  # type: ignore[arg-type]

    assert fake_signal_group.calls == [(fake_proc.pid, signal.SIGTERM)]


@posix_only
@pytest.mark.asyncio
async def test_terminate_subtree_with_grace_escalates_to_sigkill_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
    fake_proc: _FakeProc,
    fake_signal_group: _FakeSignalGroup,
) -> None:
    """SIGTERM ignored past grace → SIGKILL fires.

    Production trigger: a hung gcc that doesn't honour SIGTERM.
    The user-visible contract is "Stop kills the build" — if the
    SIGKILL branch ever drops, the runner loop hangs waiting for
    a process that won't exit and the queue gets wedged.
    """

    async def _raise_timeout(awaitable: Any, *_args: Any, **_kwargs: Any) -> None:
        # Close the awaitable so a "coroutine was never awaited"
        # RuntimeWarning doesn't fire when GC reaps the unstarted
        # ``proc.wait()`` coroutine the helper passed in.
        if hasattr(awaitable, "close"):
            awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(
        "esphome_device_builder.helpers.process.asyncio.wait_for",
        _raise_timeout,
    )

    await terminate_subtree_with_grace(
        fake_proc,  # type: ignore[arg-type]
        grace_seconds=0.01,
        job_label="job test-1",
    )

    assert fake_signal_group.calls == [
        (fake_proc.pid, signal.SIGTERM),
        (fake_proc.pid, signal.SIGKILL),
    ]


@posix_only
@pytest.mark.asyncio
async def test_terminate_subtree_with_grace_returns_when_sigterm_undelivered(
    monkeypatch: pytest.MonkeyPatch,
    fake_proc: _FakeProc,
    fake_signal_group: _FakeSignalGroup,
) -> None:
    """``_signal_process_group`` returning False (proc gone) short-circuits.

    No point waiting for a grace window or escalating to SIGKILL
    if the SIGTERM never landed — the pid is already gone.
    """
    fake_signal_group.return_value = False
    waited = False

    async def _record_wait_for(*_args: Any, **_kwargs: Any) -> None:  # pragma: no cover
        nonlocal waited
        waited = True

    monkeypatch.setattr(
        "esphome_device_builder.helpers.process.asyncio.wait_for",
        _record_wait_for,
    )

    await terminate_subtree_with_grace(fake_proc)  # type: ignore[arg-type]

    assert waited is False


@windows_only
@pytest.mark.asyncio
async def test_terminate_subtree_with_grace_falls_back_to_proc_kill_on_taskkill_failure(
    monkeypatch: pytest.MonkeyPatch,
    fake_proc: _FakeProc,
) -> None:
    """Windows: taskkill returning False triggers the ``proc.kill()`` fallback.

    ``taskkill`` may be missing (stripped from the image) or hung;
    the fallback ensures the parent at least dies so the runner
    loop can finalise the job. The ``proc.kill()`` is wrapped in
    ``kill_quietly`` so a race with the child's own exit doesn't
    raise.
    """

    async def _fake_terminate_subtree(_pid: int) -> bool:
        return False

    monkeypatch.setattr(
        "esphome_device_builder.helpers.process._terminate_subtree_windows",
        _fake_terminate_subtree,
    )

    await terminate_subtree_with_grace(fake_proc)  # type: ignore[arg-type]

    assert fake_proc.kill_calls == 1
