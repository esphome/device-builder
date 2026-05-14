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
        # Two events kept in lockstep. ``_has_subscriber`` is set
        # while count > 0; ``_no_subscribers`` is set while
        # count == 0. Tracking both lets consumers ``await`` on
        # *either* transition — the ICMP loop awaits subscriber
        # presence before each sweep AND awaits the no-subscriber
        # event during its post-sweep idle window so a
        # subscriber-drop mid-sleep cuts straight to the
        # ``wait_for_subscriber`` park instead of burning the rest
        # of the interval. Without that, a subscriber arriving
        # ~1ms after the last one left could wait up to
        # ``_PING_INTERVAL`` for fresh ICMP data.
        self._has_subscriber = asyncio.Event()
        self._no_subscribers = asyncio.Event()
        self._no_subscribers.set()  # initial state: gate is closed

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

    async def wait_for_no_subscribers(self) -> None:
        """Suspend until the count drops to 0.

        Mirror of :meth:`wait_for_subscriber` for the opposite
        transition — used by consumers whose idle wait should be
        interruptible by a subscriber drop. The ICMP ping loop
        wraps its post-sweep idle window in
        ``asyncio.wait_for(presence.wait_for_no_subscribers(),
        timeout=_PING_INTERVAL)`` so when the last subscriber
        disconnects the loop short-circuits the rest of the
        interval and parks at the top on ``wait_for_subscriber``
        — keeping the next subscriber's first sweep within one
        scheduling tick of their connect.
        """
        await self._no_subscribers.wait()

    @contextmanager
    def subscriber(self) -> Iterator[None]:
        """
        Context manager that increments the count for its body.

        The 0→1 transition sets ``_has_subscriber`` and clears
        ``_no_subscribers`` so any awaiter on
        :meth:`wait_for_subscriber` resumes; the 1→0 transition
        does the inverse and wakes any awaiter on
        :meth:`wait_for_no_subscribers`. The count is decremented
        in ``finally`` so the gate closes even if the wrapped
        body raises.
        """
        self._count += 1
        if self._count == 1:
            self._has_subscriber.set()
            self._no_subscribers.clear()
        try:
            yield
        finally:
            self._count -= 1
            if self._count == 0:
                self._has_subscriber.clear()
                self._no_subscribers.set()
