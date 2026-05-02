"""Simple synchronous event bus."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from typing import Any

from ..models import EventType

_LOGGER = logging.getLogger(__name__)


@dataclass
class Event:
    """A device builder event."""

    event_type: EventType
    data: dict[str, Any]


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

    def fire(self, event_type: EventType, data: dict[str, Any] | None = None) -> None:
        """Fire an event to all listeners."""
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
