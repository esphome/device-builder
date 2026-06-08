"""Tests for the per-file :mod:`helpers.storage` writer."""

from __future__ import annotations

import asyncio
import stat
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from esphome_device_builder.helpers.atomic_io import read_bytes_with_retry
from esphome_device_builder.helpers.storage import ShutdownCallback, Store


def _identity_encoder(value: bytes) -> bytes:
    return value


def _identity_decoder(raw: bytes) -> bytes:
    return raw


@dataclass
class _Recorder:
    """Captures encode/decode invocations + tracked shutdown callbacks.

    Mirrors the shape a real consumer would have: a list of
    shutdown callbacks the lifecycle layer walks at stop time, and
    test-side bookkeeping for assertions.
    """

    shutdown_callbacks: list[ShutdownCallback] = field(default_factory=list)


@pytest.fixture
def recorder() -> _Recorder:
    return _Recorder()


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "data.json"


@pytest.fixture
def store(recorder: _Recorder, store_path: Path) -> Store[bytes]:
    return Store[bytes](
        store_path,
        encoder=_identity_encoder,
        decoder=_identity_decoder,
        shutdown_register=recorder.shutdown_callbacks.append,
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


async def test_async_load_returns_none_when_file_missing(store: Store[bytes]) -> None:
    """A non-existent file yields ``None`` rather than raising."""
    assert await store.async_load() is None


async def test_async_load_decodes_existing_file(store_path: Path, store: Store[bytes]) -> None:
    """An existing file is read + handed through the decoder."""
    store_path.write_bytes(b"hello")
    assert await store.async_load() == b"hello"


async def test_async_delay_save_writes_after_delay(store_path: Path, store: Store[bytes]) -> None:
    """A scheduled save lands on disk after the delay elapses."""
    store.async_delay_save(lambda: b"delayed", delay=0.05)
    # 2s rather than the 1s default; busy Windows xdist workers can take
    # longer than 1s for the executor hop + atomic rename after the 50ms
    # timer fires.
    await _drain_loop_until(store_path.exists, timeout=2.0)
    assert await asyncio.to_thread(read_bytes_with_retry, store_path) == b"delayed"


async def test_async_delay_save_collapses_within_window(
    store_path: Path, store: Store[bytes]
) -> None:
    """Multiple calls within the debounce window emit one write of the latest state.

    ``data_func`` is called at flush time, not at scheduling time —
    the persisted value reflects the consumer's in-RAM state at
    flush, not whatever existed when the first call queued the
    handle.
    """
    state = bytearray(b"v0")

    def _capture() -> bytes:
        return bytes(state)

    store.async_delay_save(_capture, delay=0.1)
    state[:] = b"v1"
    store.async_delay_save(_capture, delay=0.1)
    state[:] = b"final"
    store.async_delay_save(_capture, delay=0.1)

    await _drain_loop_until(store_path.exists, timeout=2.0)
    assert await asyncio.to_thread(read_bytes_with_retry, store_path) == b"final"


async def test_async_delay_save_extends_deadline_on_later_call(
    store_path: Path, store: Store[bytes]
) -> None:
    """A later ``async_delay_save`` extends the deadline rather than firing earlier.

    Mirrors HA's extend semantics: if a call requests a write
    further in the future than the existing handle, the existing
    handle still fires first but reschedules itself to the later
    deadline. Net effect — the latest requested write time wins.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()

    store.async_delay_save(lambda: b"early", delay=0.05)
    store.async_delay_save(lambda: b"late", delay=0.20)

    await _drain_loop_until(store_path.exists, timeout=2.0)
    elapsed = loop.time() - started
    assert elapsed >= 0.18, f"wrote too early: {elapsed:.3f}s"
    assert await asyncio.to_thread(read_bytes_with_retry, store_path) == b"late"


async def test_async_delay_save_earlier_call_replaces_handle(
    store_path: Path, store: Store[bytes]
) -> None:
    """A call requesting an earlier deadline cancels the existing handle.

    The threshold sits well below the 'later' deadline so the
    timing distinguishes 'replaced' from 'fired the later
    handle'. Generous slack (1.5s) covers Windows-CI's slow
    timer granularity + executor-hop + atomic-rename latency
    on a noisy xdist worker — the previous 0.45s threshold
    flaked at ~0.7s elapsed on the Windows runner even when
    the earlier handle correctly replaced the later one.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    store.async_delay_save(lambda: b"later", delay=3.0)
    store.async_delay_save(lambda: b"earlier", delay=0.05)

    await _drain_loop_until(store_path.exists, timeout=4.0)
    elapsed = loop.time() - started
    assert elapsed < 1.5, f"earlier handle did not replace later: {elapsed:.3f}s"
    assert await asyncio.to_thread(read_bytes_with_retry, store_path) == b"earlier"


async def test_async_save_now_flushes_pending_save(store_path: Path, store: Store[bytes]) -> None:
    """``async_save_now`` cancels the timer + writes immediately."""
    store.async_delay_save(lambda: b"pending", delay=10.0)
    assert not store_path.exists()
    await store.async_save_now()
    assert await asyncio.to_thread(read_bytes_with_retry, store_path) == b"pending"


async def test_async_save_now_is_noop_when_empty(store_path: Path, store: Store[bytes]) -> None:
    """Calling on an empty store is idempotent."""
    await store.async_save_now()
    await store.async_save_now()
    assert not store_path.exists()


async def test_async_save_now_awaits_inflight_write(tmp_path: Path) -> None:
    """Concurrent in-flight write completes before the final flush issues.

    Race: the delayed-handler-driven write is mid-executor when
    ``async_save_now`` runs. Without the await, both writes would
    race on the lock and the final flush could observe ``data_func
    is None`` and silently no-op, dropping a mutation queued AFTER
    the in-flight write. ``threading.Event`` (not ``asyncio.Event``)
    because the encoder runs in a real executor thread.
    """
    seen: list[bytes] = []
    write_started = threading.Event()
    release_write = threading.Event()

    def _slow_encoder(value: bytes) -> bytes:
        seen.append(value)
        write_started.set()
        release_write.wait(timeout=2.0)
        return value

    store_path = tmp_path / "data.json"
    store = Store[bytes](
        store_path,
        encoder=_slow_encoder,
        decoder=_identity_decoder,
        shutdown_register=lambda _cb: None,
    )

    store.async_delay_save(lambda: b"first", delay=0.0)
    # Wait for the executor thread to enter the encoder.
    await asyncio.get_running_loop().run_in_executor(None, write_started.wait, 2.0)
    # Queue a follow-up that should land AFTER the in-flight write.
    store.async_delay_save(lambda: b"second", delay=10.0)
    flush_task = asyncio.create_task(store.async_save_now())
    # Give the flush task a chance to await the in-flight write.
    await asyncio.sleep(0.05)
    release_write.set()
    await flush_task

    assert seen == [b"first", b"second"]
    # Final disk content reflects the *last* write.
    assert await asyncio.to_thread(read_bytes_with_retry, store_path) == b"second"


async def test_write_failure_logged_and_swallowed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A write that raises is logged but doesn't crash the loop.

    Pins the diagnostic shape: the log line includes both *name*
    and *path* so production traces identify which store failed
    without the caller plumbing its own context.
    """

    def _raising_encoder(_value: bytes) -> bytes:
        msg = "boom"
        raise RuntimeError(msg)

    store_path = tmp_path / "data.json"
    store = Store[bytes](
        store_path,
        encoder=_raising_encoder,
        decoder=_identity_decoder,
        shutdown_register=lambda _cb: None,
        name="pairings_test_store",
    )

    with caplog.at_level("ERROR"):
        store.async_delay_save(lambda: b"x", delay=0.0)
        await _drain_loop_until(
            lambda: any("Error writing store" in r.message for r in caplog.records),
            timeout=1.0,
        )

    matching = [r for r in caplog.records if "Error writing store" in r.message]
    assert matching, "expected error log line"
    assert "pairings_test_store" in matching[0].getMessage()
    assert str(store_path) in matching[0].getMessage()


async def test_default_name_falls_back_to_path(tmp_path: Path) -> None:
    """Omitting *name* derives a stand-in from the store path."""
    store_path = tmp_path / "data.json"
    store = Store[bytes](
        store_path,
        encoder=_identity_encoder,
        decoder=_identity_decoder,
        shutdown_register=lambda _cb: None,
    )
    assert str(store_path) in store._name


async def test_data_func_called_at_flush_not_scheduling(
    store_path: Path, store: Store[bytes]
) -> None:
    """``data_func`` is invoked at flush, capturing post-schedule mutations."""
    state = bytearray(b"initial")

    def _read() -> bytes:
        return bytes(state)

    store.async_delay_save(_read, delay=0.05)
    state[:] = b"after-mutation"
    # 2s rather than the 1s default; busy Windows xdist workers can take
    # longer than 1s for the executor hop + atomic rename after the 50ms
    # timer fires.
    await _drain_loop_until(store_path.exists, timeout=2.0)
    assert await asyncio.to_thread(read_bytes_with_retry, store_path) == b"after-mutation"


async def test_atomic_write_creates_parent_directory(tmp_path: Path) -> None:
    """The store creates missing parent directories rather than failing."""
    store_path = tmp_path / "subdir" / "nested" / "data.json"
    store = Store[bytes](
        store_path,
        encoder=_identity_encoder,
        decoder=_identity_decoder,
        shutdown_register=lambda _cb: None,
    )
    store.async_delay_save(lambda: b"nested", delay=0.0)
    # 2s rather than the 1s used by the simple-write tests below;
    # the parent-directory mkdir on top of the atomic write can tip
    # this past 1s on loaded Windows runners.
    await _drain_loop_until(store_path.exists, timeout=2.0)
    assert await asyncio.to_thread(read_bytes_with_retry, store_path) == b"nested"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows doesn't honor POSIX mode bits")
async def test_default_mode_is_owner_only(tmp_path: Path) -> None:
    """The default ``mode=0o600`` keeps persisted state owner-only.

    Pins the security-relevant default — pinned receiver pubkeys,
    peer identities, future API tokens are all routed through
    ``Store`` and shouldn't be readable by group / other.
    """
    store_path = tmp_path / "data.json"
    store = Store[bytes](
        store_path,
        encoder=_identity_encoder,
        decoder=_identity_decoder,
        shutdown_register=lambda _cb: None,
    )
    store.async_delay_save(lambda: b"sensitive", delay=0.0)
    await _drain_loop_until(store_path.exists, timeout=1.0)
    assert stat.S_IMODE(store_path.stat().st_mode) == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="Windows doesn't honor POSIX mode bits")
async def test_explicit_mode_is_applied(tmp_path: Path) -> None:
    """Caller-supplied *mode* lands on the persisted file.

    Non-sensitive consumers (e.g. a public catalog snapshot) can
    opt into ``0o644`` so other dashboard processes can read it
    without sudo.
    """
    store_path = tmp_path / "data.json"
    store = Store[bytes](
        store_path,
        encoder=_identity_encoder,
        decoder=_identity_decoder,
        shutdown_register=lambda _cb: None,
        mode=0o644,
    )
    store.async_delay_save(lambda: b"public", delay=0.0)
    await _drain_loop_until(store_path.exists, timeout=1.0)
    assert stat.S_IMODE(store_path.stat().st_mode) == 0o644


async def test_atomic_write_cleans_up_tempfile_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed ``os.replace`` doesn't leave orphaned ``.tmp`` files behind.

    The atomic-write path stages bytes in a sibling tempfile then
    ``os.replace``s into place. If ``replace`` raises (disk full,
    permissions, EXDEV), the tempfile must be unlinked rather
    than accumulating one ``.<name>.<random>.tmp`` file per
    failed write across the process's lifetime.
    """
    store_path = tmp_path / "data.json"
    store = Store[bytes](
        store_path,
        encoder=_identity_encoder,
        decoder=_identity_decoder,
        shutdown_register=lambda _cb: None,
    )

    def _fail(*_args: object, **_kwargs: object) -> None:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr("os.replace", _fail)

    store.async_delay_save(lambda: b"x", delay=0.0)
    # Wait long enough for the executor to attempt + fail the write.
    await asyncio.sleep(0.1)
    assert not store_path.exists()
    assert not list(tmp_path.glob("data.json.*.tmp"))


async def test_constructor_registers_shutdown_callback(
    recorder: _Recorder, store: Store[bytes], store_path: Path
) -> None:
    """``shutdown_register`` is invoked exactly once at construction.

    The registered callback is :meth:`async_save_now` itself —
    awaiting it from the registry's flush loop drains any pending
    debounced save without the caller having to thread a
    reference to the store instance through the lifecycle layer.
    """
    assert len(recorder.shutdown_callbacks) == 1
    store.async_delay_save(lambda: b"via-shutdown", delay=10.0)
    await recorder.shutdown_callbacks[0]()
    assert await asyncio.to_thread(read_bytes_with_retry, store_path) == b"via-shutdown"


async def test_lifecycle_walk_flushes_pending_saves_across_stores(
    tmp_path: Path,
) -> None:
    """Multiple stores share one shutdown registry; the walker drains all of them.

    Mirrors the production wiring where a controller's stop()
    iterates a single registry list that every owned store
    appended itself to. A pending delayed save in any of them
    must land before the lifecycle layer's stop() returns.
    """
    callbacks: list[ShutdownCallback] = []

    pairings_path = tmp_path / "pairings.json"
    peers_path = tmp_path / "peers.json"

    pairings = Store[bytes](
        pairings_path,
        encoder=_identity_encoder,
        decoder=_identity_decoder,
        shutdown_register=callbacks.append,
    )
    peers = Store[bytes](
        peers_path,
        encoder=_identity_encoder,
        decoder=_identity_decoder,
        shutdown_register=callbacks.append,
    )

    pairings.async_delay_save(lambda: b"pairings-final", delay=10.0)
    peers.async_delay_save(lambda: b"peers-final", delay=10.0)

    assert not pairings_path.exists()
    assert not peers_path.exists()

    for cb in callbacks:
        await cb()

    assert await asyncio.to_thread(read_bytes_with_retry, pairings_path) == b"pairings-final"
    assert await asyncio.to_thread(read_bytes_with_retry, peers_path) == b"peers-final"


async def test_save_now_skips_when_no_pending_data_after_drain(
    store_path: Path, store: Store[bytes]
) -> None:
    """If a save already drained the data, a follow-up flush is a no-op."""
    store.async_delay_save(lambda: b"v", delay=0.0)
    await _drain_loop_until(store_path.exists, timeout=1.0)
    await store.async_save_now()
    # Disk content unchanged — the second flush had nothing to do.
    assert await asyncio.to_thread(read_bytes_with_retry, store_path) == b"v"


async def test_extend_then_save_now_picks_up_latest(store_path: Path, store: Store[bytes]) -> None:
    """A flush after multiple extending calls writes the latest state once."""
    store.async_delay_save(lambda: b"a", delay=10.0)
    store.async_delay_save(lambda: b"b", delay=10.0)
    store.async_delay_save(lambda: b"c", delay=10.0)
    await store.async_save_now()
    assert await asyncio.to_thread(read_bytes_with_retry, store_path) == b"c"


async def test_handle_write_returns_early_when_data_func_already_drained(
    recorder: _Recorder, store: Store[bytes], store_path: Path
) -> None:
    """Direct ``_async_handle_write`` call with no captured data is a no-op.

    Defense-in-depth against a concurrent flush draining the
    data_func between handle-schedule and the write task entering
    its critical section. Production paths don't hit it under
    normal scheduling, but the branch matters if a future caller
    composes the helper differently.
    """
    assert store._data_func is None
    await store._async_handle_write()
    assert not store_path.exists()


async def test_load_propagates_decoder_errors(tmp_path: Path) -> None:
    """A corrupt file surfaces the decoder exception rather than masking.

    A consumer that wants soft-recovery wraps ``async_load`` in
    its own try/except — the helper deliberately doesn't pretend
    silently-empty state is fine.
    """

    def _strict_decoder(_raw: bytes) -> bytes:
        msg = "corrupt"
        raise ValueError(msg)

    store_path = tmp_path / "data.json"
    store_path.write_bytes(b"garbage")
    store = Store[bytes](
        store_path,
        encoder=_identity_encoder,
        decoder=_strict_decoder,
        shutdown_register=lambda _cb: None,
    )

    with pytest.raises(ValueError, match="corrupt"):
        await store.async_load()


async def test_path_property_exposes_store_path(store_path: Path, store: Store[bytes]) -> None:
    """The ``path`` property surfaces the store's on-disk location."""
    assert store.path == store_path
