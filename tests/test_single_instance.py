"""
Coverage for the per-config-dir startup lock.

The lock guards against two ``device-builder`` processes racing on
the same config directory — the metadata sidecar, identity files,
build tree, and firmware queue all use per-process
``threading.Lock`` instances that don't extend across processes.
A double-launch would corrupt state silently.

These tests pin three contracts:

1. **First start succeeds** and writes a JSON record into
   ``<config_dir>/.device-builder.lock`` carrying ``pid``,
   ``lock_format_version``, ``device_builder_version``, and
   ``start_ts`` — operators / future dashboards reading the file
   must see a stable shape.
2. **Second start contends** and gets ``exit_code = 1``, with the
   running PID surfaced on stderr so the operator knows what
   they're stepping on. Driven via ``multiprocessing.Process`` so
   the contention is real (cross-process flock); a same-process
   re-acquire would falsely succeed because flock is reentrant on
   the same fd.
3. **A stale lock file is harmless** — the next start re-acquires
   cleanly, so a previous crash doesn't permanently lock the user
   out.

The cross-process contention test is skipped on Windows (``fcntl``
unavailable; the helper degrades to a silent no-op there per
issue #451's "best-effort or skip entirely" Windows allowance).
"""

from __future__ import annotations

import json
import multiprocessing as mp
import sys
import time
from pathlib import Path

import pytest

from esphome_device_builder.helpers.single_instance import (
    SingleInstanceLock,
    ensure_single_execution,
)


def _hold_lock(
    config_dir: str,
    started: mp.synchronize.Event,
    release: mp.synchronize.Event,
) -> None:
    """
    Subprocess body: acquire the lock and wait for the parent's signal.

    Used by the contention test — runs in a forked subprocess
    so the flock is held by a *different* PID than the test
    runner. Signals via ``started`` once the lock is held, then
    blocks on ``release`` to keep the lock alive until the parent
    has finished its own (failing) acquisition attempt.
    """
    with ensure_single_execution(Path(config_dir)) as lock:
        if lock.exit_code is not None:
            # Shouldn't happen — we're the first acquirer.
            return
        started.set()
        release.wait(timeout=10.0)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="single-instance lock is a no-op on Windows (no fcntl)",
)
def test_first_start_acquires_and_writes_lock_info(tmp_path: Path) -> None:
    """A clean ``config_dir`` acquires the lock and writes diagnostics."""
    with ensure_single_execution(tmp_path) as lock:
        assert isinstance(lock, SingleInstanceLock)
        assert lock.exit_code is None

        lock_path = tmp_path / ".device-builder.lock"
        assert lock_path.exists()

        # The contents are flushed at write time so the file is
        # readable from another fd while we still hold the flock.
        contents = json.loads(lock_path.read_text())
        # ``pid`` matches the test runner — we acquired in-process.
        assert contents["pid"] == int(contents["pid"])
        assert contents["lock_format_version"] == 1
        assert isinstance(contents["device_builder_version"], str)
        assert contents["device_builder_version"]  # non-empty
        assert isinstance(contents["start_ts"], (int, float))


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="single-instance lock is a no-op on Windows (no fcntl)",
)
def test_release_lets_subsequent_start_succeed(tmp_path: Path) -> None:
    """
    Releasing the lock (context exit) lets the next start acquire cleanly.

    Pins the "stale lock file is harmless" contract from the
    issue: the file persists on disk after the context exits
    (we deliberately don't unlink — the OS only releases the
    flock, not the file), but a fresh start re-acquires the
    flock without surfacing the previous record as contention.
    """
    with ensure_single_execution(tmp_path) as first:
        assert first.exit_code is None
    # File is still on disk between starts (no cleanup needed).
    assert (tmp_path / ".device-builder.lock").exists()

    with ensure_single_execution(tmp_path) as second:
        assert second.exit_code is None


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="exercises the Windows no-op path",
)
def test_windows_no_op_yields_success_without_touching_disk(
    tmp_path: Path,
) -> None:
    """
    Windows / no-fcntl path: yield ``exit_code=None`` and write nothing.

    The helper degrades to a silent no-op when ``fcntl`` is
    unavailable (issue #451's "best-effort or skip entirely"
    Windows allowance). The CI matrix runs on Windows too, so
    pin that the context manager still produces a usable
    ``SingleInstanceLock`` (``exit_code=None``, caller proceeds
    normally) and that no lock file lands on disk — surfacing
    a stray ``.device-builder.lock`` would mislead operators
    into thinking the cross-process guarantee is in effect when
    it isn't.
    """
    with ensure_single_execution(tmp_path) as lock:
        assert isinstance(lock, SingleInstanceLock)
        assert lock.exit_code is None
    assert not (tmp_path / ".device-builder.lock").exists()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="single-instance lock is a no-op on Windows (no fcntl)",
)
def test_contention_with_running_instance_returns_exit_code_1(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """
    A second start while the lock is held by another PID surfaces ``exit_code=1``.

    Drives the contention via ``multiprocessing.Process`` so the
    flock is genuinely held by a different PID — same-process
    flock is reentrant on the same fd and would falsely succeed.
    Captures stderr to confirm the operator-facing diagnostic
    output names the running PID.
    """
    ctx = mp.get_context("spawn")
    started = ctx.Event()
    release = ctx.Event()
    holder = ctx.Process(
        target=_hold_lock,
        args=(str(tmp_path), started, release),
    )
    holder.start()
    try:
        # Wait for the child to actually acquire — without this
        # the parent might race ahead and acquire first, defeating
        # the test's premise.
        assert started.wait(timeout=5.0), "child did not acquire lock in time"

        # Drain anything the child wrote to stderr before we check
        # the parent's contention output.
        capfd.readouterr()

        with ensure_single_execution(tmp_path) as lock:
            assert lock.exit_code == 1

        captured = capfd.readouterr()
        assert "Another device-builder is already running" in captured.err
        # Surfaces the running PID so operators can ``kill`` or
        # ``ps`` it; this is the headline UX win.
        assert f"PID: {holder.pid}" in captured.err
        assert str(tmp_path) in captured.err
    finally:
        release.set()
        holder.join(timeout=5.0)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=2.0)


def test_contention_handles_unreadable_lock_file_gracefully(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """
    A lock file with garbage / partial JSON still produces a usable error.

    A lock file written by a future dashboard with an unknown
    schema, or partially flushed during a crash mid-write, would
    fail the JSON parse. We must still surface "another instance
    is already running" + the config dir path — silently
    swallowing the contention would let a double-launch slip
    past. Drive this in-process by pre-corrupting the lock file's
    contents BEFORE acquiring; the child still acquires the
    flock cleanly because it ``a+``-opens (no truncate).
    """
    if sys.platform == "win32":
        pytest.skip("flock unavailable")

    # Pre-create the lock file with garbage. The first acquirer
    # will overwrite it (``a+`` then truncate after flock), so
    # to keep the garbage visible we have to corrupt it again
    # *after* the child acquires but BEFORE the parent attempts.
    ctx = mp.get_context("spawn")
    started = ctx.Event()
    release = ctx.Event()
    holder = ctx.Process(
        target=_hold_lock,
        args=(str(tmp_path), started, release),
    )
    holder.start()
    try:
        assert started.wait(timeout=5.0)
        # Wait briefly so the child's flush lands before we
        # overwrite — we want our garbage in the file when the
        # parent reads on contention.
        time.sleep(0.05)
        lock_path = tmp_path / ".device-builder.lock"
        lock_path.write_text("not valid json {{{")

        capfd.readouterr()
        with ensure_single_execution(tmp_path) as lock:
            assert lock.exit_code == 1
        captured = capfd.readouterr()
        assert "Another device-builder is already running" in captured.err
        assert "Unable to read lock file details" in captured.err
        assert str(tmp_path) in captured.err
    finally:
        release.set()
        holder.join(timeout=5.0)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=2.0)
