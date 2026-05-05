"""
Reference-counted "is anyone watching?" gate.

A small primitive consumers (the ICMP ping loop, periodic mDNS
refresh, MQTT discover-publish cadence) use to pause idle-time
traffic while no dashboard client is subscribed. The
``subscribe_events`` stream wraps its main body in
:meth:`SubscriberPresence.subscriber` so the count tracks the live
WS clients exactly; the 0→1 transition wakes every awaiter on
:meth:`wait_for_subscriber` so first-load latency is bounded by the
consumer's own loop cost, not its configured idle interval.

Lives in its own module rather than on :class:`EventBus` because
the two concerns are independent — the bus is about delivering
events to listeners, presence is about lifecycle of the dashboard
WS clients. Splitting it lets other consumers gate on presence
without taking a dependency on the bus, and keeps each class's
responsibilities single-purpose.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager


class SubscriberPresence:
    """
    Reference-counted dashboard-subscriber gate.

    Single-task design: every mutation runs synchronously on the
    event-loop thread, so the count and the asyncio.Event don't need
    a lock. Awaiters resume the next time the loop runs after the
    0→1 transition; the 1→0 transition re-arms the gate so a fresh
    awaiter parks again.
    """

    def __init__(self) -> None:
        self._count = 0
        self._has_subscriber = asyncio.Event()

    def has_subscribers(self) -> bool:
        """Return True while at least one subscriber is registered."""
        return self._count > 0

    @property
    def count(self) -> int:
        """Current subscriber count — exposed for tests / metrics."""
        return self._count

    async def wait_for_subscriber(self) -> None:
        """Suspend until at least one subscriber is registered.

        Returns immediately when the gate is already open. Awaiters
        block again only after the count drops back to 0 and they
        come around for the next iteration of their loop.
        """
        await self._has_subscriber.wait()

    @contextmanager
    def subscriber(self) -> Iterator[None]:
        """
        Context manager that increments the count for its body.

        The 0→1 transition sets the internal asyncio.Event so any
        awaiter on :meth:`wait_for_subscriber` resumes; the 1→0
        transition clears it so the next awaiter parks again. The
        count is decremented in ``finally`` so the gate closes
        even if the wrapped body raises.
        """
        self._count += 1
        if self._count == 1:
            self._has_subscriber.set()
        try:
            yield
        finally:
            self._count -= 1
            if self._count == 0:
                self._has_subscriber.clear()
