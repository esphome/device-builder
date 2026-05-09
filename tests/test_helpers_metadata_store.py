"""Tests for the debounced :mod:`helpers.metadata_store` writer."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from esphome_device_builder.helpers.metadata_store import (
    MetadataKeyStore,
    ShutdownCallback,
)


@dataclass
class _FakeBackend:
    """In-memory stand-in for the metadata sidecar.

    Records how many times the writer ran + the latest payload, so
    tests can assert debounce collapsing without touching disk.
    Bundles the shutdown callback list so a test can both inspect
    *that the store registered* and run the registered callbacks
    to simulate the lifecycle layer's ``stop()`` behaviour.
    """

    config_dir: Path
    writes: list[object]
    shutdown_callbacks: list[ShutdownCallback] = field(default_factory=list)
    initial_value: object | None = None

    def load(self, config_dir: Path) -> object:
        assert config_dir == self.config_dir
        return self.initial_value

    def write(self, config_dir: Path, value: object) -> None:
        assert config_dir == self.config_dir
        self.writes.append(value)

    async def run_shutdown_callbacks(self) -> None:
        """Mimic the lifecycle layer awaiting every registered store."""
        for cb in self.shutdown_callbacks:
            await cb()


@pytest.fixture
def backend(tmp_path: Path) -> _FakeBackend:
    return _FakeBackend(config_dir=tmp_path, writes=[])


@pytest.fixture
def store(backend: _FakeBackend) -> MetadataKeyStore[object]:
    return MetadataKeyStore[object](
        backend.config_dir,
        load_sync=backend.load,
        write_sync=backend.write,
        shutdown_register=backend.shutdown_callbacks.append,
    )


async def _drain_loop_until(predicate: Callable[[], bool], timeout: float = 1.0) -> None:
    """Yield to the loop until *predicate()* is true or *timeout* elapses."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    msg = f"timed out waiting for predicate after {timeout}s"
    raise AssertionError(msg)


@pytest.mark.asyncio
async def test_async_load_returns_initial_value(
    backend: _FakeBackend, store: MetadataKeyStore[object]
) -> None:
    """``async_load`` hops to the executor + returns the loader's output."""
    backend.initial_value = {"pairings": [1, 2, 3]}
    result = await store.async_load()
    assert result == {"pairings": [1, 2, 3]}


@pytest.mark.asyncio
async def test_async_delay_save_writes_after_delay(
    backend: _FakeBackend, store: MetadataKeyStore[object]
) -> None:
    """A scheduled save lands after the delay elapses."""
    store.async_delay_save(lambda: {"v": 1}, delay=0.05)
    await _drain_loop_until(lambda: bool(backend.writes))
    assert backend.writes == [{"v": 1}]


@pytest.mark.asyncio
async def test_async_delay_save_collapses_within_window(
    backend: _FakeBackend, store: MetadataKeyStore[object]
) -> None:
    """Multiple calls within the debounce window emit one write of the latest state.

    ``data_func`` is called at flush time, not at scheduling time —
    so the persisted value reflects the controller's in-RAM state at
    flush, not whatever existed when the first call queued the
    handle. This is the property that lets a controller call
    ``async_delay_save`` cheaply on every mutation without worrying
    about coalescing.
    """
    state = {"v": 0}

    def _capture() -> dict[str, int]:
        return dict(state)

    store.async_delay_save(_capture, delay=0.1)
    state["v"] = 1
    store.async_delay_save(_capture, delay=0.1)
    state["v"] = 2
    store.async_delay_save(_capture, delay=0.1)

    await _drain_loop_until(lambda: bool(backend.writes), timeout=2.0)
    # Only one write, with the LATEST state.
    assert backend.writes == [{"v": 2}]


@pytest.mark.asyncio
async def test_async_delay_save_extends_deadline_on_later_call(
    backend: _FakeBackend, store: MetadataKeyStore[object]
) -> None:
    """A later ``async_delay_save`` extends the deadline rather than firing earlier.

    Mirrors HA's extend semantics: if a call requests a write
    further in the future than the existing handle, the existing
    handle still fires first but reschedules itself to the later
    deadline. Net effect — the latest requested write time wins.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()

    store.async_delay_save(lambda: "early", delay=0.05)
    # Same scheduling thread; second call extends.
    store.async_delay_save(lambda: "late", delay=0.20)

    await _drain_loop_until(lambda: bool(backend.writes), timeout=2.0)
    elapsed = loop.time() - started
    # We should NOT have written before the *later* deadline.
    assert elapsed >= 0.18, f"wrote too early: {elapsed:.3f}s"
    assert backend.writes == ["late"]


@pytest.mark.asyncio
async def test_async_delay_save_earlier_call_replaces_handle(
    backend: _FakeBackend, store: MetadataKeyStore[object]
) -> None:
    """A call requesting an earlier deadline cancels the existing handle."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    store.async_delay_save(lambda: "later", delay=0.50)
    store.async_delay_save(lambda: "earlier", delay=0.05)

    await _drain_loop_until(lambda: bool(backend.writes), timeout=1.0)
    elapsed = loop.time() - started
    assert elapsed < 0.45
    assert backend.writes == ["earlier"]


@pytest.mark.asyncio
async def test_async_save_now_flushes_pending_save(
    backend: _FakeBackend, store: MetadataKeyStore[object]
) -> None:
    """``async_save_now`` cancels the timer + writes immediately."""
    store.async_delay_save(lambda: "pending", delay=10.0)
    assert backend.writes == []
    await store.async_save_now()
    assert backend.writes == ["pending"]


@pytest.mark.asyncio
async def test_async_save_now_is_noop_when_empty(
    backend: _FakeBackend, store: MetadataKeyStore[object]
) -> None:
    """Calling on an empty store is idempotent."""
    await store.async_save_now()
    await store.async_save_now()
    assert backend.writes == []


@pytest.mark.asyncio
async def test_async_save_now_awaits_inflight_write(tmp_path: Path) -> None:
    """Concurrent in-flight write completes before the final flush issues.

    Race: the delayed-handler-driven write is mid-executor when
    ``async_save_now`` runs. Without the await, both writes would
    race on the lock and the final flush could observe ``data_func
    is None`` and silently no-op, dropping a mutation queued AFTER
    the in-flight write. ``threading.Event`` (not ``asyncio.Event``)
    because the writer runs in a real executor thread.
    """
    seen: list[str] = []
    write_started = threading.Event()
    release_write = threading.Event()

    def _slow_write(_config_dir: Path, value: object) -> None:
        seen.append(str(value))
        write_started.set()
        release_write.wait(timeout=2.0)

    def _load(_config_dir: Path) -> object:  # pragma: no cover
        msg = "load not used in this test"
        raise AssertionError(msg)

    store = MetadataKeyStore[object](
        tmp_path,
        load_sync=_load,
        write_sync=_slow_write,
        shutdown_register=lambda _cb: None,
    )

    store.async_delay_save(lambda: "first", delay=0.0)
    # Wait for the executor thread to enter the write.
    await asyncio.get_running_loop().run_in_executor(None, write_started.wait, 2.0)
    # Queue a follow-up that should land AFTER the in-flight write.
    store.async_delay_save(lambda: "second", delay=10.0)
    flush_task = asyncio.create_task(store.async_save_now())
    # Give the flush task a chance to await the in-flight write.
    await asyncio.sleep(0.05)
    release_write.set()
    await flush_task

    assert seen == ["first", "second"]


@pytest.mark.asyncio
async def test_write_failure_logged_and_swallowed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A write that raises is logged but doesn't crash the loop.

    Also pins the diagnostic shape: the log line includes both
    *name* and *config_dir* so production traces identify which
    sub-key failed without the caller plumbing its own context.
    """

    def _raising_write(_config_dir: Path, _value: object) -> None:
        msg = "boom"
        raise OSError(msg)

    def _load(_config_dir: Path) -> object:  # pragma: no cover
        msg = "load not used in this test"
        raise AssertionError(msg)

    store = MetadataKeyStore[object](
        tmp_path,
        load_sync=_load,
        write_sync=_raising_write,
        shutdown_register=lambda _cb: None,
        name="_pairings_test_key",
    )

    with caplog.at_level("ERROR"):
        store.async_delay_save(lambda: "x", delay=0.0)
        await _drain_loop_until(
            lambda: any("Error writing metadata key" in r.message for r in caplog.records),
            timeout=1.0,
        )

    matching = [r for r in caplog.records if "Error writing metadata key" in r.message]
    assert matching, "expected error log line"
    assert "_pairings_test_key" in matching[0].getMessage()
    assert str(tmp_path) in matching[0].getMessage()


@pytest.mark.asyncio
async def test_default_name_falls_back_to_config_dir(tmp_path: Path) -> None:
    """Omitting *name* derives a stand-in from ``config_dir``.

    Belt-and-braces so a caller that forgets to pass *name* still
    gets *some* identifying string in error logs / asyncio task
    names rather than a bare ``"metadata-store-write:None"``.
    """

    def _no_load(_config_dir: Path) -> object:  # pragma: no cover
        msg = "load not used in this test"
        raise AssertionError(msg)

    def _noop_write(_config_dir: Path, _value: object) -> None:  # pragma: no cover
        pass

    store = MetadataKeyStore[object](
        tmp_path,
        load_sync=_no_load,
        write_sync=_noop_write,
        shutdown_register=lambda _cb: None,
    )
    # Internal but stable: the derived name embeds config_dir so a
    # forgotten *name* still gives an operator a starting point.
    assert str(tmp_path) in store._name


@pytest.mark.asyncio
async def test_data_func_called_at_flush_not_scheduling(
    backend: _FakeBackend, store: MetadataKeyStore[object]
) -> None:
    """``data_func`` is invoked at flush, capturing post-schedule mutations."""
    state = {"counter": 0}

    def _read() -> dict[str, int]:
        # Reads at flush time. If we read at scheduling time the
        # write would land with counter=0.
        return dict(state)

    store.async_delay_save(_read, delay=0.05)
    state["counter"] = 99
    await _drain_loop_until(lambda: bool(backend.writes), timeout=1.0)
    assert backend.writes == [{"counter": 99}]


@pytest.mark.asyncio
async def test_load_sync_runs_in_executor(
    tmp_path: Path,
) -> None:
    """``async_load`` doesn't block the event loop on the loader."""

    def _slow_load(config_dir: Path) -> str:
        # Sentinel for "ran on a thread" — pure-sync function;
        # ``run_in_executor`` is what makes this safe.
        assert config_dir == tmp_path
        return "loaded"

    def _write(_config_dir: Path, _value: object) -> None:  # pragma: no cover
        msg = "should not be called"
        raise AssertionError(msg)

    store = MetadataKeyStore[str](
        tmp_path,
        load_sync=_slow_load,
        write_sync=_write,
        shutdown_register=lambda _cb: None,
    )
    assert await store.async_load() == "loaded"


@pytest.mark.asyncio
async def test_extend_then_save_now_picks_up_latest(
    backend: _FakeBackend, store: MetadataKeyStore[object]
) -> None:
    """A flush after multiple extending calls writes the latest state once."""
    store.async_delay_save(lambda: "a", delay=10.0)
    store.async_delay_save(lambda: "b", delay=10.0)
    store.async_delay_save(lambda: "c", delay=10.0)
    await store.async_save_now()
    assert backend.writes == ["c"]


@pytest.mark.asyncio
async def test_save_now_skips_when_no_pending_data_after_drain(
    backend: _FakeBackend, store: MetadataKeyStore[object]
) -> None:
    """If a save already drained the data, a follow-up flush is a no-op."""
    store.async_delay_save(lambda: "v", delay=0.0)
    await _drain_loop_until(lambda: bool(backend.writes), timeout=1.0)
    await store.async_save_now()
    # Still only one write — the second flush had nothing to do.
    assert backend.writes == ["v"]


@pytest.mark.asyncio
async def test_constructor_registers_shutdown_callback(
    backend: _FakeBackend, store: MetadataKeyStore[object]
) -> None:
    """``shutdown_register`` is invoked exactly once at construction.

    The registered callback is :meth:`async_save_now` itself —
    awaiting it from the registry's flush loop drains any pending
    debounced save without the caller having to thread a reference
    to the store instance through the lifecycle layer.
    """
    assert len(backend.shutdown_callbacks) == 1
    # The registered callback IS ``async_save_now``; awaiting it
    # should drain a queued save just like a direct call.
    store.async_delay_save(lambda: "via-shutdown", delay=10.0)
    await backend.shutdown_callbacks[0]()
    assert backend.writes == ["via-shutdown"]


@pytest.mark.asyncio
async def test_lifecycle_walk_flushes_pending_saves_across_stores(
    tmp_path: Path,
) -> None:
    """Multiple stores share one shutdown registry; the walker drains all of them.

    Mirrors the production wiring where ``DeviceBuilder.stop()``
    iterates a single registry list that every controller's stores
    appended themselves to. A pending delayed save in any of them
    must land before the lifecycle-layer's ``stop()`` returns.
    """
    callbacks: list[ShutdownCallback] = []

    pairings_writes: list[object] = []
    peers_writes: list[object] = []

    def _no_load(_config_dir: Path) -> object:  # pragma: no cover
        msg = "load not used in this test"
        raise AssertionError(msg)

    def _pairings_write(_config_dir: Path, value: object) -> None:
        pairings_writes.append(value)

    def _peers_write(_config_dir: Path, value: object) -> None:
        peers_writes.append(value)

    pairings = MetadataKeyStore[object](
        tmp_path,
        load_sync=_no_load,
        write_sync=_pairings_write,
        shutdown_register=callbacks.append,
    )
    peers = MetadataKeyStore[object](
        tmp_path,
        load_sync=_no_load,
        write_sync=_peers_write,
        shutdown_register=callbacks.append,
    )

    # Both stores have unflushed delayed saves with long deadlines.
    pairings.async_delay_save(lambda: "pairings-final", delay=10.0)
    peers.async_delay_save(lambda: "peers-final", delay=10.0)

    assert pairings_writes == []
    assert peers_writes == []

    # Lifecycle-layer drain.
    for cb in callbacks:
        await cb()

    assert pairings_writes == ["pairings-final"]
    assert peers_writes == ["peers-final"]


@pytest.mark.asyncio
async def test_handle_write_returns_early_when_data_func_already_drained(
    backend: _FakeBackend, store: MetadataKeyStore[object]
) -> None:
    """Direct ``_async_handle_write`` call with no captured data is a no-op.

    The early-return is defense-in-depth against a concurrent
    flush draining the data_func between handle-schedule and the
    write task entering its critical section. Production paths
    don't hit it under normal scheduling, but the branch matters
    if a future caller composes the helper differently (a manual
    ``_async_handle_write`` after a successful ``async_save_now``).
    """
    # Pre-condition: nothing scheduled, no captured func.
    assert store._data_func is None
    await store._async_handle_write()
    assert backend.writes == []
