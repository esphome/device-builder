"""
Per-file debounced JSON store, modelled on Home Assistant's ``Store``.

Each :class:`Store` owns one JSON file end to end — read,
atomic write, debounced save, shutdown flush — and the typed
value lives in the consumer's RAM. The atomic write is a
``write-tmp + os.replace`` dance via
:func:`helpers.atomic_io.atomic_write`.

Per-file rather than wrapping a sub-key of a shared sidecar so
domain writes don't take each other out on a corrupt write,
backup / restore is per concern, and there's no lock contention
between unrelated writers.

Consumers inject *encoder* / *decoder* so this layer stays
agnostic about the wire format. mashumaro dataclasses pair with
``encoder=lambda v: orjson.dumps(v.to_dict())`` /
``decoder=lambda b: SomeClass.from_dict(orjson.loads(b))``.

Behaviour kept from HA:

* **Debounce + extend semantics.** Calls during an open delay
  window update the *latest* deadline rather than firing
  immediately; the timer reschedules itself to the latest
  requested write time when it wakes early.
* **Captured data_func at write time.** The caller hands a
  zero-arg callable producing the current value; we call it
  inside the write critical section, so a mutation between
  ``async_delay_save`` and the flush picks up the latest
  in-RAM state.
* **Mandatory shutdown registration.** The constructor takes a
  *shutdown_register* callback invoked once with
  :meth:`async_save_now`; the caller's lifecycle layer holds
  the resulting list and ``await``s every callback during
  graceful stop. Required (not optional) so a store can't be
  instantiated without telling someone who will flush it.
  ``SIGKILL`` / process crash skip the registry, same as HA's
  ``EVENT_HOMEASSISTANT_FINAL_WRITE``; pending in-RAM
  mutations are lost.

Typical use::

    import orjson

    self._store: Store[OffloaderRemoteBuildSettings] = Store(
        config_dir / "_offloader_remote_build.json",
        encoder=lambda v: orjson.dumps(v.to_dict()),
        decoder=lambda b: OffloaderRemoteBuildSettings.from_dict(orjson.loads(b)),
        shutdown_register=self._shutdown_callbacks.append,
    )
    # On every mutation:
    self._store.async_delay_save(self._serialize_pairings, delay=1.0)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path

from .async_ import run_in_executor
from .atomic_io import atomic_write

_LOGGER = logging.getLogger(__name__)

# PEP 695 ``type`` aliases so call sites don't spell
# ``Callable[[Callable[[], Awaitable[None]]], None]`` inline.
type ShutdownCallback = Callable[[], Awaitable[None]]
type ShutdownRegister = Callable[[ShutdownCallback], None]


class Store[T]:
    """
    Debounced per-file JSON writer.

    Owns no in-RAM state of its own — the caller holds the live
    value and hands us a *data_func* that serialises it on
    demand. *T* is the value type the *data_func* returns and
    :meth:`async_load` yields back from disk.
    """

    def __init__(
        self,
        path: Path,
        *,
        encoder: Callable[[T], bytes],
        decoder: Callable[[bytes], T],
        shutdown_register: ShutdownRegister,
        name: str | None = None,
        mode: int = 0o600,
    ) -> None:
        """
        Bind the store to *path* + caller-supplied codec hooks.

        *shutdown_register* is invoked exactly once during
        construction with :meth:`async_save_now`; the caller's
        lifecycle layer is then responsible for awaiting that
        callback at graceful shutdown. Simplest valid value is
        ``some_list.append``; tests that don't care can pass
        ``lambda _cb: None``.

        *name* is a diagnostic label for the write-task name +
        error log lines; defaults to *path*. *mode* is the
        POSIX mode applied to the file + staging tempfile
        (``0o600`` — owner-only — by default since the dominant
        consumers hold cryptographic state).
        """
        self._path = path
        self._encoder = encoder
        self._decoder = decoder
        self._name = name or str(path)
        self._mode = mode
        # Captured at every ``async_delay_save`` call; invoked
        # at flush time inside the write lock so the value
        # reflects the latest in-RAM state.
        self._data_func: Callable[[], T] | None = None
        self._delay_handle: asyncio.TimerHandle | None = None
        self._next_write_time = 0.0
        # Single-flight writes against this file: prevents a
        # ``stop()``-triggered ``async_save_now`` from racing a
        # delayed-handler-triggered write and losing the
        # consumer's latest mutation.
        self._write_lock = asyncio.Lock()
        self._inflight_write: asyncio.Task[None] | None = None
        shutdown_register(self.async_save_now)

    @property
    def path(self) -> Path:
        """The on-disk path this store owns."""
        return self._path

    async def async_load(self) -> T | None:
        """Read + decode the file. Returns ``None`` if it doesn't exist.

        Single-shot read intended for consumer start; the
        in-RAM value is the source of truth from then on.
        Decoder failures propagate — a corrupt file means the
        consumer needs an explicit recovery decision rather
        than silently starting from empty state.
        """
        return await run_in_executor(self._load_sync)

    def _load_sync(self) -> T | None:
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return None
        return self._decoder(raw)

    def async_delay_save(self, data_func: Callable[[], T], delay: float = 0.0) -> None:
        """Schedule a write of *data_func()*'s output after *delay* seconds.

        Calls during an open delay window extend the deadline
        to the latest requested write time; the captured
        data_func is invoked at flush time so multiple
        mutations within a single debounce window collapse to
        one write of the final state.
        """
        self._data_func = data_func
        loop = asyncio.get_running_loop()
        next_when = loop.time() + delay
        if self._delay_handle is not None and self._delay_handle.when() < next_when:
            # Existing handle fires earlier than the new
            # request; remember the later deadline and let the
            # handle reschedule itself when it wakes.
            self._next_write_time = next_when
            return
        if self._delay_handle is not None:
            self._delay_handle.cancel()
        self._next_write_time = next_when
        self._delay_handle = loop.call_at(next_when, self._on_delay_handle_fire)

    def _on_delay_handle_fire(self) -> None:
        """Sync timer callback; reschedule or kick off the actual write."""
        loop = asyncio.get_running_loop()
        if loop.time() < self._next_write_time:
            # A later ``async_delay_save`` extended the
            # deadline while this handle was sitting in the
            # loop; reschedule to the new target.
            self._delay_handle = loop.call_at(self._next_write_time, self._on_delay_handle_fire)
            return
        self._delay_handle = None
        self._inflight_write = asyncio.create_task(
            self._async_handle_write(), name=f"store-write:{self._name}"
        )

    async def _async_handle_write(self) -> None:
        """Run one write under the lock; clear the captured data_func.

        ``data_func()`` runs on the event loop (typically a
        fast in-RAM snapshot); ``encoder()`` +
        :func:`atomic_io.atomic_write` run together in one
        executor hop so the encoder's meaningful work + the
        synchronous file I/O don't pay two hops.
        """
        async with self._write_lock:
            data_func = self._data_func
            self._data_func = None
            if data_func is None:
                # Concurrent ``async_save_now`` already drained
                # the captured func; nothing to write.
                return
            try:
                value = data_func()
                await run_in_executor(self._encode_and_write, value)
            except Exception:
                # Background-task write failures shouldn't
                # propagate — the consumer's mutation is still
                # in RAM, the next mutation reschedules a save,
                # and an unwind here would be noisy through
                # asyncio task machinery.
                _LOGGER.exception("Error writing store %s at %s", self._name, self._path)

    def _encode_and_write(self, value: T) -> None:
        """Encode + atomic-write inside a single executor hop."""
        payload = self._encoder(value)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self._path, payload, mode=self._mode)

    async def async_save_now(self) -> None:
        """Cancel any pending delay + flush whatever's queued.

        Used from the consumer's ``stop()`` so a debounced
        save scheduled microseconds before shutdown still
        lands on disk. Awaits any in-flight executor write
        first so back-to-back stop / shutdown paths don't
        interleave. Idempotent.
        """
        if self._delay_handle is not None:
            self._delay_handle.cancel()
            self._delay_handle = None
        if self._inflight_write is not None and not self._inflight_write.done():
            # Let the earlier delayed-handler-triggered write
            # finish first so we don't run two writer tasks
            # back-to-back. The post-snapshot flush below picks
            # up any data_func captured *after* the in-flight
            # write started. Errors were already logged inside
            # ``_async_handle_write``; suppress so the
            # post-snapshot flush still runs.
            with suppress(Exception):
                await self._inflight_write
        if self._data_func is not None:
            await self._async_handle_write()
