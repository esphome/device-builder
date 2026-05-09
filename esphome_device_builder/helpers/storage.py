"""Per-file debounced JSON store, modelled on Home Assistant's ``Store``.

Vendored down from HA (see ``~/home-assistant/homeassistant/helpers/storage.py``)
and adapted to our case. Each :class:`Store` owns one JSON file end
to end — read, atomic write, debounced save, shutdown flush — and
the typed value lives in the consumer's RAM. The atomic write is a
``write-tmp + os.replace`` dance; corrupting a partial write
doesn't leave a half-written file in place.

Diverges from HA in scope-trims that don't earn their complexity
here: no ``hass.bus`` / ``EVENT_HOMEASSISTANT_FINAL_WRITE`` (we
require a *shutdown_register* callback the caller supplies — see
the constructor docstring), no ``_StoreManager`` preload cache (a
single read at startup is fast enough), no version migration (the
schema lives in the typed dataclass and we ship breaking changes
through the normal upgrade path while in alpha), no
``_load_future`` reentrancy guard (we load once at controller
start), no ``read_only`` / ``private`` knobs (no consumer needs
them yet — add later if one does).

Why per-file rather than wrapping a sub-key of a shared sidecar:

* Atomic writes are per-domain — corrupting the offloader pairings
  file can't take out the device list.
* Independent backup / restore per concern.
* No lock contention between unrelated writers.
* Closer to HA muscle memory (the reference implementation is
  per-file).
* Easier to "blow it all away" reset story per-domain.

Consumers inject *encoder* / *decoder* so this layer stays
agnostic about the wire format. mashumaro dataclasses pair with
``encoder=lambda v: orjson.dumps(v.to_dict())`` /
``decoder=lambda b: SomeClass.from_dict(orjson.loads(b))``.

Keeps the parts of HA's design that earn their complexity:

* **Debounce + extend semantics matching HA.** Calls during an
  open delay window update the *latest* deadline rather than
  firing immediately; the timer reschedules itself to the latest
  requested write time when it wakes early. Mirrors HA's
  ``Store.async_delay_save`` bit-for-bit so a future reader with
  HA muscle memory isn't surprised.
* **Lock-protected write hop.** The disk write hands off to the
  default executor via ``run_in_executor``; an ``asyncio.Lock``
  serialises overlapping flushes against the same file. Without
  it a ``stop()`` flush could race with a still-pending delayed
  write.
* **Captured data_func at write time.** The caller hands us a
  zero-arg callable that produces the current value to persist;
  we call it inside the write critical section, so a mutation
  that lands between ``async_delay_save`` and the eventual flush
  picks up the latest in-RAM state.
* **Mandatory shutdown registration.** The constructor takes a
  *shutdown_register* callback that's invoked exactly once with
  ``self.async_save_now`` at construction. The caller's lifecycle
  layer holds the resulting list and ``await``s every callback
  during graceful stop, so a debounced save scheduled
  microseconds before shutdown always lands. Required (not
  optional) so a store can't be instantiated without telling
  someone who will flush it. Caveats: ``SIGKILL`` / process
  crash skip the registry the same way HA's
  ``EVENT_HOMEASSISTANT_FINAL_WRITE`` skips on hard kills; pending
  in-RAM mutations are lost. Persistence under crashes would
  require an after-every-mutation write, defeating the debounce;
  for our use case (paired-receivers list, similar low-frequency
  state) the trade is accepted.

Typical use::

    import orjson

    from ..helpers.storage import Store

    def _encode(value: OffloaderRemoteBuildSettings) -> bytes:
        return orjson.dumps(value.to_dict())

    def _decode(raw: bytes) -> OffloaderRemoteBuildSettings:
        return OffloaderRemoteBuildSettings.from_dict(orjson.loads(raw))

    self._store: Store[OffloaderRemoteBuildSettings] = Store(
        config_dir / "_offloader_remote_build.json",
        encoder=_encode,
        decoder=_decode,
        shutdown_register=self._shutdown_callbacks.append,
    )

    # On every mutation:
    self._pairings[key] = pairing
    self._store.async_delay_save(self._serialize_pairings, delay=1.0)

    # ``async_save_now`` is also still available for callers that
    # want a synchronous flush outside the shutdown path.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path

from .atomic_io import atomic_write

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


class Store[T]:
    """Debounced per-file JSON writer.

    Owns no in-RAM state of its own — the caller holds the live
    value (``_pairings`` etc.) and hands us a *data_func* that
    serialises it on demand. We track only the pending write
    deadline + timer so the disk write can be debounced.

    Per-instance, single file. Spinning up two instances pointing
    at the same file is supported (each runs its own debounce) but
    redundant; production has one per consumer.

    *T* is the value type the consumer's *data_func* returns and
    the value *async_load* yields back from disk; the store is
    agnostic to its shape (typically a mashumaro dataclass).
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
        """Bind the store to *path* + caller-supplied codec hooks.

        *encoder* takes a value of type *T* and returns its on-disk
        bytes (typically ``orjson.dumps(value.to_dict())`` for a
        mashumaro dataclass). *decoder* is the inverse: takes the
        on-disk bytes and returns *T*.

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

        *name* is a free-form diagnostic label attached to the
        write task and to error log lines so production failure
        traces identify which store failed without the caller
        having to ship its own context through. Optional; defaults
        to *path*.

        *mode* is the POSIX file mode applied to the persisted
        file (and the staging tempfile). Defaults to ``0o600``
        (owner read/write only) because the dominant consumers are
        cryptographic state — pinned receiver pubkeys, peer
        identities, future API tokens — none of which should be
        readable by group or other. Override for a non-sensitive
        consumer (e.g. ``0o644`` for a public catalog snapshot).
        Ignored on Windows; ``os.chmod`` only honours the
        write-bit there, which the default already keeps writeable
        for the owning user.
        """
        self._path = path
        self._encoder = encoder
        self._decoder = decoder
        self._name = name or str(path)
        self._mode = mode
        # Captured at every ``async_delay_save`` call; the actual
        # invocation happens at flush time inside the write lock so
        # the value reflects the latest in-RAM state.
        self._data_func: Callable[[], T] | None = None
        self._delay_handle: asyncio.TimerHandle | None = None
        self._next_write_time = 0.0
        # Single-flight writes against this file. Without it, a
        # ``stop()``-triggered ``async_save_now`` could land while
        # a delayed-handler-triggered write is mid-executor; the
        # second would observe ``_data_func is None`` and return
        # early, losing the consumer's latest mutation.
        self._write_lock = asyncio.Lock()
        # Latest in-flight write task, if any. ``async_save_now``
        # awaits this before issuing its own final write so the
        # two don't interleave.
        self._inflight_write: asyncio.Task[None] | None = None
        # Self-registration with the caller's lifecycle layer.
        # Done last so a misbehaving registry that synchronously
        # calls the callback (which would race a half-initialised
        # ``self``) at least sees a fully-built object — it's
        # legal but odd; production registries are list ``.append``
        # which never invokes the callback at registration time.
        shutdown_register(self.async_save_now)

    @property
    def path(self) -> Path:
        """The on-disk path this store owns."""
        return self._path

    async def async_load(self) -> T | None:
        """Read + decode the file. Returns ``None`` if it doesn't exist.

        Single-shot read intended for consumer start; the in-RAM
        value is the source of truth from then on. Hops to the
        default executor so the load doesn't block the event
        loop. Decoder failures propagate — a corrupt file means
        the consumer needs an explicit recovery decision rather
        than silently starting from empty state.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._load_sync)

    def _load_sync(self) -> T | None:
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return None
        return self._decoder(raw)

    def async_delay_save(self, data_func: Callable[[], T], delay: float = 0.0) -> None:
        """Schedule a write of *data_func()*'s output after *delay* seconds.

        Calls during an open delay window extend the deadline to
        the latest requested write time (matches HA's
        ``Store.async_delay_save``). The data_func is captured
        each call but only invoked at flush time, so the persisted
        snapshot reflects the consumer's in-RAM state at flush
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
            # A later ``async_delay_save`` extended the deadline
            # while this handle was sitting in the loop; reschedule
            # to the new target instead of firing now. Mirrors
            # HA's ``_async_schedule_callback_delayed_write``.
            self._delay_handle = loop.call_at(self._next_write_time, self._on_delay_handle_fire)
            return
        self._delay_handle = None
        self._inflight_write = asyncio.create_task(
            self._async_handle_write(), name=f"store-write:{self._name}"
        )

    async def _async_handle_write(self) -> None:
        """Run one write under the lock; clear the captured data_func.

        Splits the work between threads:

        * ``data_func()`` runs on the event loop. It's typically a
          fast snapshot of in-RAM state (e.g. a dataclass
          construction); pulling it out of the executor keeps
          ordering simple and lets the data_func close over
          consumer state without thread-safety concerns.
        * ``encoder()`` + ``_atomic_write()`` run inside one
          executor call. The encoder can do meaningful work
          (orjson serialisation of a large dict) and the file I/O
          is unconditionally synchronous; bundling them avoids
          two executor hops and keeps the loop responsive even
          when the encoder blocks (e.g. tests using a
          ``threading.Event`` to gate the write).
        """
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
                await loop.run_in_executor(None, self._encode_and_write, value)
            except Exception:
                # Disk-write failures shouldn't propagate out of a
                # background task — the consumer's mutation is
                # still in RAM (next mutation will reschedule a
                # save) and a crash here would unwind through the
                # asyncio task machinery noisily. Mirrors HA's
                # swallow of WriteError / SerializationError in
                # ``_async_handle_write_data``. Includes *name* +
                # path so production traces identify which store
                # broke.
                _LOGGER.exception("Error writing store %s at %s", self._name, self._path)

    def _encode_and_write(self, value: T) -> None:
        """Encode + atomic-write inside a single executor hop."""
        payload = self._encoder(value)
        self._atomic_write(payload)

    def _atomic_write(self, payload: bytes) -> None:
        """Persist *payload* atomically at the configured mode.

        Delegates to :func:`helpers.atomic_io.atomic_write` for the
        actual ``mkstemp`` + ``chmod`` + ``os.replace`` dance — that
        helper already handles the rare ``os.fdopen`` failure path
        (closes the raw fd to avoid leaking it before the with-block
        manages the file) and the on-error tempfile cleanup. We just
        ensure the parent directory exists first; ``atomic_write``
        assumes it does.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self._path, payload, mode=self._mode)

    async def async_save_now(self) -> None:
        """Cancel any pending delay + flush whatever's queued.

        Used from the consumer's ``stop()`` so a debounced save
        scheduled microseconds before shutdown still lands on
        disk. Awaits any in-flight executor write before issuing
        its own, so back-to-back stop / shutdown paths don't
        interleave. Idempotent — calling on an empty store is a
        no-op.
        """
        if self._delay_handle is not None:
            self._delay_handle.cancel()
            self._delay_handle = None
        if self._inflight_write is not None and not self._inflight_write.done():
            # An earlier delayed handler already kicked off a
            # write; let it complete so the executor isn't running
            # two writer tasks back-to-back. The second write
            # below picks up any data_func captured *after* the
            # in-flight write started. Errors were already logged
            # inside ``_async_handle_write``; suppress so the
            # post-snapshot flush still runs.
            with suppress(Exception):
                await self._inflight_write
        if self._data_func is not None:
            await self._async_handle_write()
