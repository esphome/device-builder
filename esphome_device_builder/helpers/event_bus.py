"""Simple synchronous event bus."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from functools import partial
from typing import Any

from ..models import EventType

_LOGGER = logging.getLogger(__name__)

# Bound for the per-follower bus → client queue. Any client falling
# this far behind has its newest events dropped at ``put_nowait`` so
# the synchronous ``bus.fire`` returns immediately and the producer
# (firmware runner, mDNS callback, …) keeps making progress. The
# alternative — an unbounded queue — let a single backpressured
# websocket accumulate every line of a runaway compile in memory.
_DEFAULT_STREAM_QUEUE_MAX = 4000


@dataclass
class Event:
    """
    A device builder event.

    ``data`` is typed as a read-only :class:`Mapping` so per-event
    :class:`TypedDict` payloads are accepted at the fire site
    without a ``cast()``. ``TypedDict`` is structurally compatible
    with ``Mapping[str, Any]`` (read-only access), which is all
    subscribers ever do — every ``event.data`` reference in the
    codebase is a ``.get(...)`` or ``[key]`` lookup, never a
    mutation. Mirrors HA's ``Event[_DataT]`` pattern's *result*
    (typed access without casts) without paying for the full
    generic infrastructure HA needs in core.
    """

    event_type: EventType
    data: Mapping[str, Any]


class EventBus:
    """Simple synchronous event bus for dashboard state changes."""

    def __init__(self) -> None:
        self._listeners: dict[EventType, set[Callable[[Event], None]]] = {}

    def add_listener(
        self, event_type: EventType, listener: Callable[[Event], None]
    ) -> Callable[[], None]:
        """Add a listener. Returns an unsubscribe callback."""
        self._listeners.setdefault(event_type, set()).add(listener)
        return partial(self._remove_listener, event_type, listener)

    def _remove_listener(self, event_type: EventType, listener: Callable[[Event], None]) -> None:
        self._listeners.get(event_type, set()).discard(listener)

    def fire(self, event_type: EventType, data: Mapping[str, Any] | None = None) -> None:
        """
        Fire an event to all listeners.

        ``data`` is :class:`Mapping[str, Any]` so per-event
        :class:`TypedDict` payloads pass through without a cast —
        ``payload: SomeEventData = {...}; bus.fire(EventType.X,
        payload)`` type-checks. See :class:`Event` for the rest
        of the rationale.
        """
        event = Event(event_type, data or {})
        for listener in list(self._listeners.get(event_type, set())):
            try:
                listener(event)
            except Exception:
                _LOGGER.exception("Event listener raised an exception")

    @contextmanager
    def listening(
        self,
        event_types: Iterable[EventType],
        listener: Callable[[Event], None],
    ) -> Iterator[None]:
        """
        Subscribe *listener* to every event in *event_types* for the block.

        Replaces the four-line ``unsub_X = bus.add_listener(...)`` +
        ``finally: for u in unsubs: u()`` boilerplate every multi-event
        subscription site was repeating. Each ``add_listener`` call
        returns an unsubscribe callable; the context manager runs all
        of them on exit (success or failure) so a partially-attached
        subscription doesn't leak listeners on early raise.

        Multiple listeners share the same shape via stacked ``with``:

        .. code-block:: python

            with (
                bus.listening(LIFECYCLE_EVENTS, _on_lifecycle),
                bus.listening([EventType.JOB_OUTPUT], _on_output),
                bus.listening([EventType.JOB_PROGRESS], _on_progress),
            ):
                ...

        Synchronous context manager rather than async because both
        ``add_listener`` and the unsubscribe callable are sync —
        the only reason to make this async would be to await
        something during enter/exit, which we don't.
        """
        # Append per-iteration rather than via list comprehension so a
        # mid-loop ``add_listener`` raise leaves the earlier
        # subscriptions in ``unsubs`` for the ``finally`` to release.
        # A comprehension would discard the partial list on raise and
        # leak the listeners attached before the exception.
        unsubs: list[Callable[[], None]] = []
        try:
            for event_type in event_types:
                unsubs.append(self.add_listener(event_type, listener))  # noqa: PERF401
            yield
        finally:
            for unsub in unsubs:
                unsub()


# Type alias names kept short — they appear in three callbacks.
_StreamItem = tuple[str, Any]

# Internal sentinel pushed by ``push_or_terminate`` when the queue
# is full. The drain loop turns this into a
# ``StreamBackpressureError`` so the surrounding WS handler tears
# the connection down — for state-tracking streams (where silent
# drops would leave the client permanently stale with no resync
# path) crashing the connection is preferable to lossy delivery.
_TERMINATE_SENTINEL: tuple[str, None] = ("__terminate__", None)


class StreamBackpressureError(RuntimeError):
    """Raised by ``stream_events`` when a ``push_or_terminate`` overflows.

    Surfaces backpressure as a hard failure so the WS handler
    closes the connection and the frontend reconnects to get a
    fresh ``initial_state`` snapshot. Used by streams whose
    correctness depends on every message landing
    (``subscribe_events``, where each event represents a state
    transition the UI tracks); streams where lossy delivery is
    fine (``follow_job`` output, ``follow_jobs`` log lines) keep
    using ``push`` and accept the drop.
    """


@dataclass
class StreamControls:
    """Push primitives handed to ``stream_events`` callbacks.

    Four semantics are exposed because the right policy depends on
    what kind of message is being pushed:

    - ``push(name, payload)`` — best-effort. Drops the new item on
      ``QueueFull`` so synchronous ``bus.fire`` returns immediately
      and the producer never blocks on a slow client. Right for
      log lines, progress updates, and other content where a
      missing item is tolerable.
    - ``push_priority(name, payload)`` — guaranteed delivery. Evicts
      the oldest queued item to make room when full. Right for
      one-shot must-land events like terminal job results or
      lifecycle status transitions, where a silent drop would
      leave the UI stuck on stale state forever.
    - ``push_or_terminate(name, payload)`` — drop the *connection*
      on overflow. Pushes a terminate sentinel that makes the drain
      loop raise ``StreamBackpressureError`` and forces the WS
      handler to close. Right for state-tracking streams where
      silent loss is worse than a forced reconnect (the client
      reconnects, gets a fresh seed, is consistent again).
    - ``end()`` — push the terminal sentinel via ``push_priority``
      so the drain loop breaks even if the queue is saturated.
    """

    push: Callable[[str, Any], None]
    push_priority: Callable[[str, Any], None]
    push_or_terminate: Callable[[str, Any], None]
    end: Callable[[], None]


async def stream_events(
    *,
    client: Any,
    message_id: str,
    bus: EventBus,
    event_types: Iterable[EventType],
    handle_event: Callable[[Event, StreamControls], None],
    send_initial: Callable[[StreamControls], Awaitable[None]] | None = None,
    queue_max: int = _DEFAULT_STREAM_QUEUE_MAX,
) -> None:
    """
    Stream bus events to *client* via a bounded asyncio.Queue.

    Solves three correctness properties every WS-streaming command
    needs to get right, in one place:

    1. **Snapshot+subscribe atomicity.** Listeners attach inside
       ``bus.listening`` *before* ``send_initial`` is awaited, so
       any event fired during the initial replay queues through the
       listener and lands strictly after the initial payload — no
       silent loss between snapshot and live, no duplication.
    2. **Bounded memory.** A single bounded ``asyncio.Queue``
       (default 4000 slots) prevents a slow follower from
       accumulating every fired event in memory until disconnect.
    3. **Cleanup on cancel.** When the WS task is cancelled
       (client disconnect), the ``with`` block exits and
       ``bus.listening``'s ``finally`` releases every listener.
       No closures keep the closed client alive.

    Callers wire the bus → client mapping via two callbacks:

    - ``handle_event(event, controls)`` runs synchronously inside
      ``bus.fire`` (no awaits). It decides what — if anything — to
      enqueue for *event*, picking ``controls.push`` for normal
      events and ``controls.push_priority`` / ``controls.end`` for
      events that must land (e.g. terminal results, sentinels).
    - ``send_initial(controls)`` is awaited inside the listening
      block before draining. It can ``await client.send_event(...)``
      to seed the client and may call ``controls.end()`` to stop
      the drain immediately (e.g. terminal job: replay history then
      exit, no live drain needed).

    The drain loop runs until the sentinel ``None`` is received
    (cooperative shutdown via ``controls.end()``) or the surrounding
    task is cancelled.
    """
    queue: asyncio.Queue[_StreamItem | None] = asyncio.Queue(maxsize=queue_max)

    def _push(name: str, payload: Any) -> None:
        # Drop newest on full — slow follower, producer stays unblocked.
        with suppress(asyncio.QueueFull):
            queue.put_nowait((name, payload))

    def _force_enqueue(item: _StreamItem | None) -> None:
        # Evict oldest to make room — used for items that MUST land
        # (terminal result, sentinel, terminate marker) so the drain
        # loop always breaks.
        while True:
            try:
                queue.put_nowait(item)
                return
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    # Defensive: shouldn't happen given the
                    # synchronous listener path, but bail rather
                    # than spin if it does.
                    return

    def _push_or_terminate(name: str, payload: Any) -> None:
        try:
            queue.put_nowait((name, payload))
        except asyncio.QueueFull:
            # Backpressure exceeded — signal the drain to raise so
            # the WS handler closes the connection.
            _force_enqueue(_TERMINATE_SENTINEL)

    controls = StreamControls(
        push=_push,
        push_priority=lambda name, payload: _force_enqueue((name, payload)),
        push_or_terminate=_push_or_terminate,
        end=lambda: _force_enqueue(None),
    )

    def _on_event(event: Event) -> None:
        handle_event(event, controls)

    with bus.listening(event_types, _on_event):
        if send_initial is not None:
            await send_initial(controls)

        while True:
            item = await queue.get()
            if item is None:
                return
            if item is _TERMINATE_SENTINEL:
                msg = (
                    f"stream backpressure exceeded (queue cap {queue_max}); "
                    "client is too slow to drain — closing connection so it "
                    "can reconnect and resync"
                )
                raise StreamBackpressureError(msg)
            name, payload = item
            await client.send_event(message_id, name, payload)
