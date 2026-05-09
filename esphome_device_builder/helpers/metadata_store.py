"""
Debounced single-key writer for the shared metadata sidecar.

Vendored down from Home Assistant's ``homeassistant.helpers.storage.Store``
(see ``~/home-assistant/homeassistant/helpers/storage.py``) and adapted
for our case: HA's :class:`Store` owns its own JSON file, but our
``device-builder.json`` is a single sidecar shared across many keys
(``_remote_build``, ``_offloader_remote_build``, ``_devices``,
``_labels``, …) under one process-wide
:func:`~controllers.config.metadata_transaction` lock. So instead of
managing a file path, this store manages **one sub-key** of the shared
sidecar; the actual disk write hops through caller-supplied ``load_sync``
/ ``write_sync`` callbacks so it stays serialised against every other
writer of the same file (the callbacks own the
``metadata_transaction``).

Drops every HA dependency we don't need: no ``hass.bus`` / no
``EVENT_HOMEASSISTANT_FINAL_WRITE`` (we replace it with a mandatory
*shutdown_register* callback the caller supplies — see below), no
``_StoreManager`` preload cache (the sidecar is one read), no
version migration (the storage shape is owned by mashumaro on the
controller side), no ``_load_future`` reentrancy guard (we load
once at controller start).

Keeps the parts that earn their complexity:

* **Debounce + extend semantics matching HA.** Calls during an open
  delay window update the *latest* deadline rather than firing
  immediately; the timer reschedules itself to the latest requested
  write time when it fires too early. Mirrors HA's
  ``Store.async_delay_save`` behaviour bit-for-bit so a future reader
  with HA muscle memory isn't surprised.
* **Lock-protected write hop.** The disk write hands off to the
  default executor via ``run_in_executor``; an ``asyncio.Lock``
  serialises overlapping flushes against the same key. Without it a
  ``stop()`` flush could race with a still-pending delayed write.
* **Captured data_func at write time.** The caller hands us a
  zero-arg callable that produces the current dict to persist; we
  call it inside the write critical section, so a mutation that
  lands between ``async_delay_save`` and the eventual flush picks up
  the latest in-RAM state.
* **Mandatory shutdown registration.** The constructor takes a
  *shutdown_register* callback that's invoked *exactly once* with
  ``self.async_save_now`` at construction. The caller's lifecycle
  layer (typically :class:`DeviceBuilder.shutdown_callbacks`) holds
  the resulting list and ``await``s every callback during graceful
  stop, so a debounced save scheduled microseconds before shutdown
  always lands. Making registration a constructor parameter rather
  than a "remember to call ``stop()``" convention closes the
  forgot-to-wire-it footgun structurally — a store can't be
  instantiated without telling the lifecycle who's responsible for
  flushing it. Caveats: ``SIGKILL`` / process crash skip the
  registry the same way HA's ``EVENT_HOMEASSISTANT_FINAL_WRITE``
  skips on hard kills; in-RAM mutations not yet flushed are lost.
  Persistence under crashes would require an after-every-mutation
  write, defeating the debounce; for our use case (paired-receivers
  list, similar low-frequency state) the trade is accepted.

Typical use, paired with
:func:`~controllers.config.metadata_transaction` and a per-key
loader::

    from ..controllers.config import (
        load_offloader_remote_build_settings,
        save_offloader_remote_build_settings,
    )

    def _load(config_dir: Path) -> OffloaderRemoteBuildSettings:
        return load_offloader_remote_build_settings(config_dir)

    def _save(config_dir: Path, value: OffloaderRemoteBuildSettings) -> None:
        save_offloader_remote_build_settings(config_dir, value)

    self._store: MetadataKeyStore[OffloaderRemoteBuildSettings] = (
        MetadataKeyStore(
            config_dir=self._db.settings.config_dir,
            load_sync=_load,
            write_sync=_save,
            # Caller-owned: a list the lifecycle layer walks at stop
            # time. ``.append`` matches the
            # ``Callable[[ShutdownCallback], None]`` shape exactly.
            shutdown_register=self._db.shutdown_callbacks.append,
            # Surfaces in error logs + the asyncio task name so a
            # production failure trace identifies which sub-key broke.
            name="_offloader_remote_build",
        )
    )

    # On every mutation:
    self._pairings[key] = pairing
    self._store.async_delay_save(self._serialize_pairings, delay=1.0)

    # ``async_save_now`` is also still available for callers that
    # want a synchronous flush outside the shutdown path (e.g. on a
    # configuration import that wants the new state on disk before
    # the response goes back).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

# Type aliases for the shutdown-registry contract. ``ShutdownCallback``
# is what we hand the registry; ``ShutdownRegister`` is the registry's
# own shape (a function that accepts one). Splitting them out means
# call sites don't have to spell ``Callable[[Callable[[], Awaitable[None]]], None]``
# inline, and a future audit that switches the registry to a more
# structured type (e.g. an ``asyncio.TaskGroup`` exit hook) only
# touches the alias. PEP 695 ``type`` syntax (Python 3.12+) so the
# aliases live in their own namespace and don't need
# ``from __future__ import annotations`` evaluation tricks.
type ShutdownCallback = Callable[[], Awaitable[None]]
type ShutdownRegister = Callable[[ShutdownCallback], None]


class MetadataKeyStore[T]:
    """Debounced writer for a single key of the shared metadata sidecar.

    Owns no in-RAM state of its own — the controller holds the live
    dict (``_pairings`` etc.) and hands us a *data_func* that
    serialises it on demand. We track only the pending write
    deadline + timer so the disk write can be debounced.

    Per-instance, single key, single ``config_dir``. Spinning up two
    instances pointing at the same key is supported (each runs its
    own debounce) but redundant; production has one per controller.

    *T* is the type the caller's *data_func* returns and the type
    *load_sync* yields back from disk; the store is agnostic to its
    shape (typically a mashumaro dataclass like
    :class:`OffloaderRemoteBuildSettings`).
    """

    def __init__(
        self,
        config_dir: Path,
        *,
        load_sync: Callable[[Path], T],
        write_sync: Callable[[Path, T], None],
        shutdown_register: ShutdownRegister,
        name: str | None = None,
    ) -> None:
        """Bind the store to ``config_dir`` + caller-supplied I/O hooks.

        *load_sync* takes ``config_dir`` and returns the decoded
        value (or whatever default the caller chooses for missing
        keys). *write_sync* takes ``(config_dir, value)`` and
        atomically persists it. Both are sync (executor-bound).

        *shutdown_register* is invoked exactly once during
        construction with :meth:`async_save_now`; the caller's
        lifecycle layer is then responsible for awaiting that
        callback at graceful shutdown. The simplest valid value is
        ``some_list.append`` — the lifecycle iterates the list and
        ``await``s each entry. Required (not optional) so a store
        can't be instantiated without telling someone who will
        flush it; tests that don't care can pass ``lambda _cb:
        None`` to opt out, but production paths should always wire
        a real registry.

        *name* is a free-form diagnostic label (typically the
        sidecar sub-key, e.g. ``"_offloader_remote_build"``)
        attached to the write task and to error log lines so
        production failure traces identify *which* sidecar key
        failed without the caller having to ship its own context
        through. Optional; defaults to a stand-in derived from
        ``config_dir`` when omitted.

        Injection avoids importing ``controllers.config`` from a
        helper module — the ``controllers.config`` layer already
        owns the metadata sidecar (lock + atomic write); this store
        is just a debounced wrapper around it.
        """
        self._config_dir = config_dir
        self._load_sync_cb = load_sync
        self._write_sync_cb = write_sync
        self._name = name or f"<unnamed:{config_dir}>"
        # Captured at every ``async_delay_save`` call; the actual
        # invocation happens at flush time inside the write lock so
        # the value reflects the latest in-RAM state.
        self._data_func: Callable[[], T] | None = None
        self._delay_handle: asyncio.TimerHandle | None = None
        self._next_write_time = 0.0
        # Single-flight writes against this key. Without it, a
        # ``stop()``-triggered ``async_save_now`` could land while a
        # delayed-handler-triggered write is mid-executor; the second
        # would observe ``_data_func is None`` and return early,
        # losing the user's latest mutation.
        self._write_lock = asyncio.Lock()
        # Latest in-flight write task, if any. ``async_save_now``
        # awaits this before issuing its own final write so the two
        # don't interleave.
        self._inflight_write: asyncio.Task[None] | None = None
        # Self-registration with the caller's lifecycle layer.
        # Done last so a misbehaving registry that synchronously
        # calls the callback (which would race a half-initialised
        # ``self``) at least sees a fully-built object — it's
        # legal but odd; production registries are list ``.append``
        # which never invokes the callback at registration time.
        shutdown_register(self.async_save_now)

    async def async_load(self) -> T:
        """Load the current value at this key from disk.

        Single-shot read intended for controller start; the in-RAM
        state is the source of truth from then on. Hops to the
        default executor so the load doesn't block the event loop.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._load_sync_cb, self._config_dir)

    def async_delay_save(self, data_func: Callable[[], T], delay: float = 0.0) -> None:
        """Schedule a write of *data_func()*'s output after *delay* seconds.

        Calls during an open delay window extend the deadline to the
        latest requested write time (matches HA's
        ``Store.async_delay_save``). The data_func is captured each
        call but only invoked at flush time, so the persisted
        snapshot reflects the controller's in-RAM state at flush
        rather than at scheduling — multiple mutations within a
        single debounce window all collapse into one write of the
        final state.
        """
        self._data_func = data_func
        loop = asyncio.get_running_loop()
        next_when = loop.time() + delay
        if self._delay_handle is not None and self._delay_handle.when() < next_when:
            # Existing handle fires earlier than the new request;
            # remember the later deadline and let the handle
            # reschedule itself when it wakes (see
            # ``_on_delay_handle_fire``).
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
            # A later ``async_delay_save`` extended the deadline while
            # this handle was sitting in the loop; reschedule to the
            # new target instead of firing now. Mirrors HA's
            # ``_async_schedule_callback_delayed_write``.
            self._delay_handle = loop.call_at(self._next_write_time, self._on_delay_handle_fire)
            return
        self._delay_handle = None
        self._inflight_write = asyncio.create_task(
            self._async_handle_write(), name=f"metadata-store-write:{self._name}"
        )

    async def _async_handle_write(self) -> None:
        """Run one write under the lock; clear the captured data_func."""
        async with self._write_lock:
            data_func = self._data_func
            self._data_func = None
            if data_func is None:
                # A concurrent ``async_save_now`` already drained
                # the captured func; nothing to write.
                return
            loop = asyncio.get_running_loop()
            try:
                value = data_func()
                await loop.run_in_executor(None, self._write_sync_cb, self._config_dir, value)
            except Exception:
                # Disk-write failures shouldn't propagate out of a
                # background task — the controller's mutation is
                # still in RAM (next mutation will reschedule a
                # save) and a crash here would unwind through the
                # asyncio task machinery noisily. Mirrors HA's
                # swallow of WriteError / SerializationError in
                # ``_async_handle_write_data``. Include the store's
                # *name* + ``config_dir`` so production traces can
                # point at the failing sub-key.
                _LOGGER.exception(
                    "Error writing metadata key %s under %s",
                    self._name,
                    self._config_dir,
                )

    async def async_save_now(self) -> None:
        """Cancel any pending delay + flush whatever's queued.

        Used from the controller's ``stop()`` so a debounced save
        scheduled microseconds before shutdown still lands on disk.
        Awaits any in-flight executor write before issuing its own,
        so back-to-back stop / shutdown paths don't interleave.
        Idempotent — calling on an empty store is a no-op.
        """
        if self._delay_handle is not None:
            self._delay_handle.cancel()
            self._delay_handle = None
        if self._inflight_write is not None and not self._inflight_write.done():
            # An earlier delayed handler already kicked off a write;
            # let it complete so the executor isn't running two
            # writer callbacks back-to-back. The second write below
            # picks up any data_func captured *after* the in-flight
            # write started. Errors were already logged inside
            # ``_async_handle_write``; suppress so the post-snapshot
            # flush still runs.
            with suppress(Exception):
                await self._inflight_write
        if self._data_func is not None:
            await self._async_handle_write()
