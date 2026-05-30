"""Tests for the single-worker ``BuildSizeRefresher``.

Covers the queue-and-drain shape end-to-end: requests get
coalesced into the pending set, the worker wakes on the event,
walks one configuration at a time, fires the ``on_refreshed``
callback when the underlying helper actually changed something,
and survives per-iteration errors. The helper itself is mocked so
the test doesn't need a real build directory; what's being
validated here is the *plumbing* between ``request()`` /
``enqueue_stale_fleet()`` / the worker loop.

Synchronization uses ``asyncio.Event`` rather than sleep-poll
loops — every test signals "the worker is done with the work I
care about" via a fixture-installed callback hook so the test
just ``await``s on a single event with a generous timeout.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

from esphome_device_builder.controllers._build_size_refresher import BuildSizeRefresher
from esphome_device_builder.helpers.build_size import (
    BuildDirSignal,
    BuildSizeRefreshResult,
)

# Generous upper bound for any single worker-tick we wait on.
# Real refreshes complete in microseconds since everything's
# mocked — the timeout exists to surface deadlocks rather than
# wait-time, so a regression where the worker stops draining
# fails fast instead of hanging the suite.
_TIMEOUT = 5.0


def _make(
    tmp_path: Path,
    *,
    filenames: list[str] | None = None,
    on_refreshed=None,
    metadata: dict[str, dict[str, Any]] | None = None,
    persist_size=None,
) -> tuple[BuildSizeRefresher, list[str], list[tuple[str, BuildSizeRefreshResult]]]:
    """Build a refresher + the lists its callbacks append to.

    Returns ``(refresher, refreshed, persisted)`` — the test
    asserts on the second/third for the on_refreshed /
    persist_size hooks respectively.
    """
    refreshed: list[str] = []
    persisted: list[tuple[str, BuildSizeRefreshResult]] = []

    async def _default_callback(configuration: str) -> None:
        refreshed.append(configuration)

    def _default_persist(configuration: str, result: BuildSizeRefreshResult) -> None:
        persisted.append((configuration, result))

    snapshot = metadata if metadata is not None else {}
    refresher = BuildSizeRefresher(
        get_filenames=lambda: filenames or [],
        get_metadata_snapshot=lambda: snapshot,
        persist_size=persist_size or _default_persist,
        on_refreshed=on_refreshed or _default_callback,
    )
    return refresher, refreshed, persisted


# ----------------------------------------------------------------------
# Synchronous primitives — no worker running
# ----------------------------------------------------------------------


async def test_request_adds_to_pending_set_and_wakes_event(tmp_path: Path) -> None:
    """``request`` is sync, idempotent, and signals the wake event."""
    refresher, _, _ = _make(tmp_path)
    refresher.request("kitchen.yaml")
    refresher.request("kitchen.yaml")  # dedupe
    refresher.request("bedroom.yaml")
    assert refresher.pending == {"kitchen.yaml", "bedroom.yaml"}
    assert refresher._wake.is_set()


async def test_start_is_idempotent(tmp_path: Path) -> None:
    """A second ``start`` while the worker is alive is a no-op."""
    refresher, _, _ = _make(tmp_path)
    refresher.start()
    first_task = refresher._task
    refresher.start()
    assert refresher._task is first_task
    await refresher.stop()


async def test_stop_with_no_running_worker_is_noop(tmp_path: Path) -> None:
    """``stop`` on a never-started refresher must not raise."""
    refresher, _, _ = _make(tmp_path)
    await refresher.stop()  # no exception
    assert refresher._task is None


# ----------------------------------------------------------------------
# Worker drain loop
# ----------------------------------------------------------------------


async def test_worker_drains_pending_and_fires_on_refreshed(tmp_path: Path) -> None:
    """A request lands in the queue; the worker walks + fires the callback."""
    done = asyncio.Event()
    refreshed: list[str] = []

    async def _on_refreshed(configuration: str) -> None:
        refreshed.append(configuration)
        done.set()

    refresher, _, _ = _make(tmp_path, on_refreshed=_on_refreshed)

    def _refresh(_configuration: str, _cached: Any) -> BuildSizeRefreshResult:
        return BuildSizeRefreshResult(
            size_bytes=1024,
            signal=BuildDirSignal(dir_mtime=10, info_mtime=20),
        )

    with patch(
        "esphome_device_builder.controllers._build_size_refresher.refresh_build_size_if_stale",
        side_effect=_refresh,
    ):
        refresher.start()
        refresher.request("kitchen.yaml")
        await asyncio.wait_for(done.wait(), timeout=_TIMEOUT)
        await refresher.stop()

    assert refreshed == ["kitchen.yaml"]
    assert refresher.pending == set()


async def test_worker_skips_callback_when_refresh_returns_none(tmp_path: Path) -> None:
    """Refresh returning ``None`` (cache hit) → no ``on_refreshed`` invoke.

    Single-request shape so the assertion isn't sensitive to
    ``set.pop()`` ordering: a cache-hit refresh returns ``None``
    and the worker has to take the ``if result is None:
    continue`` branch *without* invoking the callback. A
    ``refresh_done`` event fires after the executor returns, so
    the test can drive ``stop()`` deterministically once the
    short-circuit has actually been exercised.
    """
    refreshed: list[str] = []

    async def _on_refreshed(configuration: str) -> None:
        refreshed.append(configuration)

    refresh_done = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _refresh(_configuration: str, _cached: Any):
        # ``call_soon_threadsafe`` only guarantees the event
        # gets scheduled to fire on the loop — the worker may
        # not have resumed yet when ``refresh_done.wait()``
        # returns. The follow-up ``await asyncio.sleep(0)``
        # cedes control back to the worker so the
        # ``if result is None: continue`` branch actually
        # executes before ``stop()`` cancels.
        loop.call_soon_threadsafe(refresh_done.set)

    refresher, _, _ = _make(tmp_path, on_refreshed=_on_refreshed)
    with patch(
        "esphome_device_builder.controllers._build_size_refresher.refresh_build_size_if_stale",
        side_effect=_refresh,
    ):
        refresher.start()
        refresher.request("cached.yaml")
        await asyncio.wait_for(refresh_done.wait(), timeout=_TIMEOUT)
        # Yield once so the worker actually executes the
        # ``continue`` branch before stop() cancels it.
        await asyncio.sleep(0)
        await refresher.stop()

    # The cache-hit branch must short-circuit without firing the
    # callback. A regression that drops the ``if result is None:
    # continue`` guard would invoke ``on_refreshed`` here.
    assert refreshed == []


async def test_worker_logs_and_continues_on_refresh_exception(tmp_path: Path, caplog: Any) -> None:
    """A per-iteration walk error logs + keeps the worker alive for the next item.

    Uses a log-event handler to wait for the failure log to land,
    plus a separate event for the successful follow-up — pending
    set's pop order is arbitrary, so synchronizing on both
    independently is necessary to avoid a race where the test
    asserts before either has actually run.
    """
    error_seen = asyncio.Event()
    success_seen = asyncio.Event()
    refreshed: list[str] = []

    class _LogTrap(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if "Build-size refresh failed for broken.yaml" in record.getMessage():
                error_seen.set()

    async def _on_refreshed(configuration: str) -> None:
        refreshed.append(configuration)
        success_seen.set()

    def _refresh(configuration: str, _cached: Any):
        if configuration == "broken.yaml":
            raise RuntimeError("disk on fire")
        return BuildSizeRefreshResult(
            size_bytes=42,
            signal=BuildDirSignal(dir_mtime=1, info_mtime=2),
        )

    refresher, _, _ = _make(tmp_path, on_refreshed=_on_refreshed)
    handler = _LogTrap()
    logging.getLogger("esphome_device_builder.controllers._build_size_refresher").addHandler(
        handler
    )
    caplog.set_level(logging.ERROR)
    try:
        with patch(
            "esphome_device_builder.controllers._build_size_refresher.refresh_build_size_if_stale",
            side_effect=_refresh,
        ):
            refresher.start()
            refresher.request("broken.yaml")
            refresher.request("kitchen.yaml")
            await asyncio.wait_for(error_seen.wait(), timeout=_TIMEOUT)
            await asyncio.wait_for(success_seen.wait(), timeout=_TIMEOUT)
            await refresher.stop()
    finally:
        logging.getLogger("esphome_device_builder.controllers._build_size_refresher").removeHandler(
            handler
        )

    assert refreshed == ["kitchen.yaml"]


async def test_worker_logs_and_continues_on_callback_exception(tmp_path: Path, caplog: Any) -> None:
    """``on_refreshed`` raising must not kill the worker either."""
    error_seen = asyncio.Event()
    success_seen = asyncio.Event()

    class _LogTrap(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if "on_refreshed callback failed for broken.yaml" in record.getMessage():
                error_seen.set()

    async def _bad_callback(configuration: str) -> None:
        if configuration == "broken.yaml":
            raise RuntimeError("scanner blew up")
        success_seen.set()

    def _refresh(_configuration: str, _cached: Any) -> BuildSizeRefreshResult:
        return BuildSizeRefreshResult(
            size_bytes=1,
            signal=BuildDirSignal(dir_mtime=1, info_mtime=1),
        )

    refresher, _, _ = _make(tmp_path, on_refreshed=_bad_callback)
    handler = _LogTrap()
    logging.getLogger("esphome_device_builder.controllers._build_size_refresher").addHandler(
        handler
    )
    caplog.set_level(logging.ERROR)
    try:
        with patch(
            "esphome_device_builder.controllers._build_size_refresher.refresh_build_size_if_stale",
            side_effect=_refresh,
        ):
            refresher.start()
            refresher.request("broken.yaml")
            await asyncio.wait_for(error_seen.wait(), timeout=_TIMEOUT)
            # Worker is still alive — request a second one and
            # confirm it's serviced normally.
            refresher.request("kitchen.yaml")
            await asyncio.wait_for(success_seen.wait(), timeout=_TIMEOUT)
            await refresher.stop()
    finally:
        logging.getLogger("esphome_device_builder.controllers._build_size_refresher").removeHandler(
            handler
        )


# ----------------------------------------------------------------------
# enqueue_stale_fleet — phase-A sweep wiring
# ----------------------------------------------------------------------


async def test_enqueue_stale_fleet_pushes_divergent_filenames(tmp_path: Path) -> None:
    """``find_stale_build_dirs`` returns a list → each one ends up in pending."""
    refresher, _, _ = _make(tmp_path, filenames=["a.yaml", "b.yaml", "c.yaml"])
    with patch(
        "esphome_device_builder.controllers._build_size_refresher.find_stale_build_dirs",
        return_value=["a.yaml", "c.yaml"],
    ):
        await refresher.enqueue_stale_fleet()

    assert refresher.pending == {"a.yaml", "c.yaml"}


async def test_enqueue_stale_fleet_empty_filenames_skips_executor(tmp_path: Path) -> None:
    """No configured devices → no executor handoff at all."""
    calls: list[Any] = []

    def _track(*args: Any, **_kw: Any) -> Any:
        calls.append(args)
        return []

    refresher, _, _ = _make(tmp_path, filenames=[])
    with patch(
        "esphome_device_builder.controllers._build_size_refresher.find_stale_build_dirs",
        side_effect=_track,
    ):
        await refresher.enqueue_stale_fleet()

    assert calls == []  # short-circuited before scheduling anything
    assert refresher.pending == set()


# ----------------------------------------------------------------------
# Initial fleet sweep + stop() error paths
# ----------------------------------------------------------------------


async def test_worker_logs_when_initial_fleet_sweep_raises(tmp_path: Path, caplog: Any) -> None:
    """Initial sweep raising must not kill the worker — log + carry on."""
    sweep_failed = asyncio.Event()
    success = asyncio.Event()

    class _LogTrap(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if "Initial build-size fleet sweep failed" in record.getMessage():
                sweep_failed.set()

    refreshed: list[str] = []

    async def _on_refreshed(configuration: str) -> None:
        refreshed.append(configuration)
        success.set()

    def _refresh(_configuration: str, _cached: Any) -> BuildSizeRefreshResult:
        return BuildSizeRefreshResult(
            size_bytes=1,
            signal=BuildDirSignal(dir_mtime=1, info_mtime=1),
        )

    refresher, _, _ = _make(tmp_path, filenames=["a.yaml"], on_refreshed=_on_refreshed)
    handler = _LogTrap()
    logging.getLogger("esphome_device_builder.controllers._build_size_refresher").addHandler(
        handler
    )
    caplog.set_level(logging.ERROR)
    try:
        with (
            patch(
                "esphome_device_builder.controllers._build_size_refresher.find_stale_build_dirs",
                side_effect=RuntimeError("metadata corrupt"),
            ),
            patch(
                "esphome_device_builder.controllers._build_size_refresher.refresh_build_size_if_stale",
                side_effect=_refresh,
            ),
        ):
            refresher.start()
            await asyncio.wait_for(sweep_failed.wait(), timeout=_TIMEOUT)
            # Worker is still alive — a fresh request gets serviced.
            refresher.request("a.yaml")
            await asyncio.wait_for(success.wait(), timeout=_TIMEOUT)
            await refresher.stop()
    finally:
        logging.getLogger("esphome_device_builder.controllers._build_size_refresher").removeHandler(
            handler
        )

    assert refreshed == ["a.yaml"]


async def test_stop_logs_unexpected_worker_exception(tmp_path: Path, caplog: Any) -> None:
    """Anything other than ``CancelledError`` from the worker gets logged.

    The worker's outer ``while True:`` doesn't catch errors raised
    *outside* the per-iteration ``try`` (e.g. an executor that
    explodes before reaching the per-iteration handler). Those
    propagate through the wake worker's task await during ``stop``;
    we log so the failure isn't invisible during a clean
    shutdown.
    """
    refresher, _, _ = _make(tmp_path)

    async def _failing_worker(self_: BuildSizeRefresher) -> None:
        # Block until cancelled by ``stop()``, then convert the
        # cancellation into an unexpected exception. This is what
        # an "outside the per-iteration try" failure looks like
        # at the await boundary in ``stop()``.
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise RuntimeError("worker exploded") from None

    caplog.set_level(logging.ERROR)
    with patch.object(BuildSizeRefresher, "_run_loop", _failing_worker):
        refresher.start()
        # Yield once so the worker actually starts awaiting; without
        # this, ``stop`` cancels before the task entered the try.
        await asyncio.sleep(0)
        await refresher.stop()

    assert any(
        "Worker BuildSizeRefresher failed during shutdown" in r.message for r in caplog.records
    )
