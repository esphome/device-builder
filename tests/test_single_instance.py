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
import os
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from esphome_device_builder.helpers import single_instance
from esphome_device_builder.helpers.single_instance import (
    _LOCK_FILE_NAME,
    SingleInstanceLock,
    _report_existing_instance,
    ensure_single_execution,
)

# Skip marker shared by every test that exercises the real ``fcntl``
# code path (acquire / release / contention / lock-file-content
# checks). Windows lacks ``fcntl`` so the helper degrades to a no-op
# there; those tests have nothing to assert. The Windows-only
# no-op test below uses ``sys.platform != "win32"`` instead.
_REQUIRES_FCNTL = pytest.mark.skipif(
    sys.platform == "win32",
    reason="single-instance lock is a no-op on Windows (no fcntl)",
)


def _hold_lock(
    config_dir: str,
    started: mp.synchronize.Event,
    release: mp.synchronize.Event,
) -> None:
    """
    Subprocess body: acquire the lock and wait for the parent's signal.

    Used by the contention test — runs in a spawned subprocess
    (via ``mp.get_context("spawn")``) so the flock is held by a
    *different* PID than the test runner. Signals via ``started``
    once the lock is held, then blocks on ``release`` to keep
    the lock alive until the parent has finished its own
    (failing) acquisition attempt.
    """
    with ensure_single_execution(Path(config_dir)) as lock:
        if lock.exit_code is not None:
            # Shouldn't happen — we're the first acquirer.
            return
        started.set()
        release.wait(timeout=10.0)


@contextmanager
def _lock_held_by_subprocess(
    config_dir: Path,
) -> Generator[mp.process.BaseProcess]:
    """
    Spawn a subprocess that holds the lock for the duration of the ``with``.

    Yields the running ``Process`` (so the test body can read
    ``holder.pid`` for diagnostic-output assertions) and tears
    everything down on exit: signals the child to release, waits
    for a clean join, and force-terminates if it didn't exit
    promptly. Used by both contention tests so the
    spawn / Event coordination / cleanup boilerplate lives in
    exactly one place.
    """
    ctx = mp.get_context("spawn")
    started = ctx.Event()
    release = ctx.Event()
    holder = ctx.Process(
        target=_hold_lock,
        args=(str(config_dir), started, release),
    )
    holder.start()
    try:
        # Wait for the child to actually acquire — without this
        # the parent might race ahead and acquire first, defeating
        # the test's premise.
        if not started.wait(timeout=5.0):
            raise RuntimeError("subprocess did not acquire lock in time")
        yield holder
    finally:
        release.set()
        holder.join(timeout=5.0)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=2.0)


@_REQUIRES_FCNTL
def test_first_start_acquires_and_writes_lock_info(tmp_path: Path) -> None:
    """A clean ``config_dir`` acquires the lock and writes diagnostics."""
    with ensure_single_execution(tmp_path) as lock:
        assert isinstance(lock, SingleInstanceLock)
        assert lock.exit_code is None

        lock_path = tmp_path / _LOCK_FILE_NAME
        assert lock_path.exists()

        # The contents are flushed at write time so the file is
        # readable from another fd while we still hold the flock.
        contents = json.loads(lock_path.read_text())
        # ``pid`` matches the test runner — we acquired in-process,
        # so the recorded PID is the one operators would
        # ``kill``/``ps`` to find the holder.
        assert contents["pid"] == os.getpid()
        assert isinstance(contents["pid"], int)
        assert contents["lock_format_version"] == 1
        assert isinstance(contents["device_builder_version"], str)
        assert contents["device_builder_version"]  # non-empty
        assert isinstance(contents["start_ts"], (int, float))


@_REQUIRES_FCNTL
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
    assert (tmp_path / _LOCK_FILE_NAME).exists()

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
    assert not (tmp_path / _LOCK_FILE_NAME).exists()


@_REQUIRES_FCNTL
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
    with _lock_held_by_subprocess(tmp_path) as holder:
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


@_REQUIRES_FCNTL
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
    past. Pre-corrupt the lock file's contents *after* the
    holder has flushed its diagnostic record so the parent's
    read sees the garbage, not the holder's clean JSON.
    """
    with _lock_held_by_subprocess(tmp_path):
        # Wait briefly so the holder's flush lands before we
        # overwrite — we want our garbage in the file when the
        # parent reads on contention.
        time.sleep(0.05)
        (tmp_path / _LOCK_FILE_NAME).write_text("not valid json {{{")

        capfd.readouterr()
        with ensure_single_execution(tmp_path) as lock:
            assert lock.exit_code == 1
        captured = capfd.readouterr()
        assert "Another device-builder is already running" in captured.err
        assert "Unable to read lock file details" in captured.err
        assert str(tmp_path) in captured.err


# ---------------------------------------------------------------------------
# Focused unit tests for ``_report_existing_instance`` branches that the
# subprocess-driven contention tests don't reach (empty file + tz fallback).
# Driving them via the contention path would require platform-specific
# patching of either the subprocess or the test runner's ``%Z`` output;
# direct unit tests are simpler and platform-independent.
# ---------------------------------------------------------------------------


def test_report_existing_instance_with_empty_lock_file(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """An empty lock file body still surfaces the contention diagnostic."""
    lock_path = tmp_path / _LOCK_FILE_NAME
    lock_path.write_text("")

    _report_existing_instance(lock_path, tmp_path)

    captured = capfd.readouterr()
    assert "Another device-builder is already running" in captured.err
    # No JSON to parse → fallback line, but no exception suffix
    # (empty content takes the explicit ``else`` branch, not the
    # except clause).
    assert "Unable to read lock file details." in captured.err
    assert str(tmp_path) in captured.err


def test_report_existing_instance_local_time_fallback(
    tmp_path: Path, capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    When ``strftime('%Z')`` returns empty, the start time falls back to ``(local time)``.

    Bare-metal / minimal libc setups that don't carry a timezone
    database leave ``%Z`` empty. The diagnostic should still
    print the start time, just unannotated, with a "(local time)"
    suffix so the operator knows the timestamp isn't UTC.
    """
    lock_path = tmp_path / _LOCK_FILE_NAME
    lock_path.write_text(
        json.dumps(
            {
                "pid": 99999,
                "lock_format_version": 1,
                "device_builder_version": "test-version",
                "start_ts": 1700000000.0,
            }
        )
    )

    # Patch the strftime call to return empty for ``%Z`` so we
    # exercise the fallback branch on a runner that would
    # normally return a tz string. Real datetime for everything
    # else so the YYYY-MM-DD HH:MM:SS portion still formats.
    fake_dt = MagicMock()
    fake_dt.strftime.side_effect = lambda fmt: "" if fmt == "%Z" else "2023-11-14 22:13:20"
    monkeypatch.setattr(
        single_instance,
        "datetime",
        MagicMock(fromtimestamp=lambda _ts: fake_dt),
    )

    _report_existing_instance(lock_path, tmp_path)

    captured = capfd.readouterr()
    assert "Started: 2023-11-14 22:13:20 (local time)" in captured.err
    assert "PID: 99999" in captured.err
    assert "Version: test-version" in captured.err


def test_no_op_yields_success_when_fcntl_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    ``ensure_single_execution`` is a silent no-op without ``fcntl``.

    Patches ``_HAS_FCNTL`` to ``False`` so the Windows path
    runs on POSIX runners too — without this, the no-op branch
    only ever exercises on the Windows CI matrix and stays
    invisible to coverage reports on the (otherwise green)
    Linux / macOS runs.
    """
    monkeypatch.setattr(single_instance, "_HAS_FCNTL", False)

    with ensure_single_execution(tmp_path) as lock:
        assert isinstance(lock, SingleInstanceLock)
        assert lock.exit_code is None
    assert not (tmp_path / _LOCK_FILE_NAME).exists()


# ---------------------------------------------------------------------------
# Hardening: the lock-file open path uses ``O_NOFOLLOW`` + ``S_ISREG``
# fstat to refuse anything that isn't a plain file. Both branches close
# a defense-in-depth gap (an attacker with config-dir write access could
# otherwise plant a symlink / FIFO at the lock-file path and have
# ``_write_lock_info`` truncate the link target / block on the FIFO
# read every dashboard start).
# ---------------------------------------------------------------------------


@_REQUIRES_FCNTL
def test_symlink_at_lock_file_is_refused(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """
    A symlink at ``<config_dir>/.device-builder.lock`` aborts startup.

    Without ``O_NOFOLLOW`` the dashboard would happily follow
    the link and have ``_write_lock_info`` truncate whatever
    file the link targets — turning the lock mechanism into a
    write-anywhere primitive for anyone with config-dir access.
    Pin the contract: a symlink at the path produces
    ``exit_code=1`` and an actionable log line, and no truncation
    of the link target.
    """
    target = tmp_path / "victim.txt"
    target.write_text("important contents that must not be truncated")
    (tmp_path / _LOCK_FILE_NAME).symlink_to(target)

    with (
        caplog.at_level("ERROR", logger=single_instance.__name__),
        ensure_single_execution(tmp_path) as lock,
    ):
        assert lock.exit_code == 1

    # The dashboard refused to start *and* the link target stayed
    # intact — the headline guarantee of the hardening.
    assert target.read_text() == "important contents that must not be truncated"
    assert any("Could not open lock file" in record.message for record in caplog.records)


@_REQUIRES_FCNTL
def test_non_regular_file_at_lock_path_is_refused(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """
    A FIFO at the lock-file path aborts startup, not blocks.

    Without the hardening, ``mkfifo`` at ``.device-builder.lock``
    (no privileges required) would block every dashboard start
    on ``_write_lock_info``'s text-mode write. The exact branch
    that catches the FIFO is implementation-defined — Python's
    ``open("a+")`` already rejects non-seekable streams with an
    ``OSError`` ("File or stream is not seekable"), so the
    ``OSError`` arm of the helper handles it before our
    ``fstat`` + ``S_ISREG`` backstop fires. Either way the
    user-visible contract is the same: ``exit_code=1`` and an
    error logged. Pin that contract; don't pin which branch.

    The ``fstat`` check stays as defense-in-depth for shapes
    that *do* open cleanly under ``a+`` (block devices, some
    character devices) where only ``S_ISREG`` rejects them.
    """
    fifo_path = tmp_path / _LOCK_FILE_NAME
    try:
        os.mkfifo(fifo_path)
    except (OSError, AttributeError):
        pytest.skip("mkfifo unavailable on this platform / filesystem")

    with (
        caplog.at_level("ERROR", logger=single_instance.__name__),
        ensure_single_execution(tmp_path) as lock,
    ):
        assert lock.exit_code == 1

    # Some error was logged (either "Could not open" from the
    # ``OSError`` arm or "is not a regular file" from the
    # ``fstat`` backstop). The headline guarantee is "dashboard
    # refused to start"; the message wording is internal.
    assert caplog.records, "expected an error log line for the refusal"


@_REQUIRES_FCNTL
def test_fstat_rejects_non_regular_file_after_open(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Defense-in-depth: post-open ``fstat`` + ``S_ISREG`` rejects oddballs.

    ``O_NOFOLLOW`` catches symlinks at open time; ``open("a+")``
    catches non-seekable streams (FIFOs); but block devices and
    some character devices DO open cleanly under ``a+``. The
    ``fstat`` + ``S_ISREG`` check is the backstop that refuses
    those. Driving a real block device into a unit test is
    impractical, so simulate the shape by patching
    ``stat.S_ISREG`` in the helper module to return ``False`` —
    a normal regular-file open then takes the rejection branch
    as if it had hit a non-regular file.
    """
    monkeypatch.setattr(single_instance.stat, "S_ISREG", lambda _mode: False)

    with (
        caplog.at_level("ERROR", logger=single_instance.__name__),
        ensure_single_execution(tmp_path) as lock,
    ):
        assert lock.exit_code == 1

    assert any("is not a regular file" in record.message for record in caplog.records)
