"""
Tests for the reference-counted subscriber-presence gate.

The gate is the small primitive that lets the ICMP ping loop park
while no dashboard client is subscribed (closing the regression
from the legacy dashboard's ``ping_request`` /
``self._subscribers`` pair). These tests pin the count + gate
contract a consumer relies on:

* increment on entry, decrement on exit (success or raise)
* 0→1 transition wakes every awaiter on ``wait_for_subscriber``
* 1→0 transition re-arms the gate so the next awaiter parks again
* ``has_subscribers`` mirrors the count's bool truthiness
"""

from __future__ import annotations

import asyncio

import pytest

from esphome_device_builder.helpers.subscriber_presence import SubscriberPresence


def test_initial_state_no_subscribers() -> None:
    """A fresh gate reports zero subscribers and is closed."""
    p = SubscriberPresence()
    assert p.count == 0
    assert p.has_subscribers() is False


def test_subscriber_context_increments_and_decrements() -> None:
    """Entering the context bumps the count; exit puts it back."""
    p = SubscriberPresence()
    with p.subscriber():
        assert p.count == 1
        assert p.has_subscribers() is True
        with p.subscriber():
            assert p.count == 2
        assert p.count == 1
    assert p.count == 0
    assert p.has_subscribers() is False


def test_subscriber_context_decrements_on_exception() -> None:
    """Count returns to zero even if the wrapped body raises.

    The whole point of using a context manager (rather than two
    explicit calls) is that callers can't accidentally leak
    subscribers when their body raises mid-stream — a leaked
    subscriber would keep the ICMP loop pinging forever after
    every WS disconnects with an error.
    """
    p = SubscriberPresence()

    class _BoomError(RuntimeError):
        pass

    with pytest.raises(_BoomError), p.subscriber():
        assert p.count == 1
        msg = "kaboom"
        raise _BoomError(msg)
    assert p.count == 0
    assert p.has_subscribers() is False


async def test_wait_for_subscriber_returns_immediately_when_open() -> None:
    """A subscriber already present means awaiting completes without parking."""
    p = SubscriberPresence()
    with p.subscriber():
        # Should resolve in ~no time — wrap in wait_for so the test
        # fails loudly rather than hanging if the gate is stuck closed.
        await asyncio.wait_for(p.wait_for_subscriber(), timeout=0.5)


async def test_wait_for_subscriber_parks_until_first_subscriber() -> None:
    """An awaiter blocks while count is 0 and resumes on the 0→1 transition."""
    p = SubscriberPresence()

    async def _waiter() -> None:
        await p.wait_for_subscriber()

    waiter_task = asyncio.create_task(_waiter())
    # Give the loop a chance to park the waiter.
    await asyncio.sleep(0)
    assert not waiter_task.done()

    with p.subscriber():
        await asyncio.wait_for(waiter_task, timeout=0.5)


async def test_wait_for_subscriber_parks_again_after_drop_to_zero() -> None:
    """A second awaiter created after the gate closed parks again.

    Pins the 1→0 transition's effect: the gate must re-arm so a
    fresh ICMP-loop iteration after the last subscriber leaves
    parks again instead of busy-running. Without the
    ``self._has_subscriber.clear()`` in the 1→0 path, the second
    waiter would resume immediately.
    """
    p = SubscriberPresence()

    # Cycle one subscriber in and out so the gate has been opened
    # and re-armed; the next waiter must observe the closed state.
    with p.subscriber():
        pass
    assert p.has_subscribers() is False

    async def _waiter() -> None:
        await p.wait_for_subscriber()

    waiter_task = asyncio.create_task(_waiter())
    await asyncio.sleep(0)
    assert not waiter_task.done(), "waiter should park while gate is closed"

    with p.subscriber():
        await asyncio.wait_for(waiter_task, timeout=0.5)


async def test_wait_for_subscriber_wakes_every_awaiter_on_open() -> None:
    """Multiple awaiters all resume when the count first goes 0→1.

    asyncio.Event semantics — every coroutine parked on
    ``Event.wait()`` resumes when the event is set, not just one.
    Pin so a regression that swaps to a 1-shot primitive (Future,
    Condition.notify(1)) would surface here.
    """
    p = SubscriberPresence()

    async def _waiter() -> None:
        await p.wait_for_subscriber()

    waiters = [asyncio.create_task(_waiter()) for _ in range(3)]
    await asyncio.sleep(0)
    assert all(not t.done() for t in waiters)

    with p.subscriber():
        await asyncio.wait_for(asyncio.gather(*waiters), timeout=0.5)


async def test_wait_for_no_subscribers_returns_immediately_when_count_is_zero() -> None:
    """The mirror gate is open at startup (count == 0).

    The 1→0 transition should set ``_no_subscribers`` *and* the
    initial state at construction time should already be set —
    so a consumer's first call to :meth:`wait_for_no_subscribers`
    on a fresh gate returns immediately. Pinning the initial
    state catches a regression that only sets the event on the
    transition (which would leave a fresh gate's first awaiter
    parked forever even though the count is already 0).
    """
    p = SubscriberPresence()
    await asyncio.wait_for(p.wait_for_no_subscribers(), timeout=0.5)


async def test_wait_for_no_subscribers_parks_while_subscribers_present() -> None:
    """A subscriber holding the gate open blocks the no-subscriber waiter."""
    p = SubscriberPresence()

    async def _waiter() -> None:
        await p.wait_for_no_subscribers()

    with p.subscriber():
        waiter_task = asyncio.create_task(_waiter())
        await asyncio.sleep(0)
        assert not waiter_task.done(), "waiter must park while count > 0"

    # Subscriber dropped — the 1→0 transition wakes the waiter.
    await asyncio.wait_for(waiter_task, timeout=0.5)


async def test_wait_for_no_subscribers_wakes_on_drop_to_zero() -> None:
    """Subscriber-drop wakes a parked no-subscribers waiter within one tick.

    This is the contract the ICMP ping loop's interruptible idle
    wait depends on — when the last subscriber leaves mid-sleep,
    the loop's ``asyncio.wait_for(presence.wait_for_no_subscribers
    (), timeout=_PING_INTERVAL)`` must fire promptly so the next
    subscriber's first sweep doesn't wait out the rest of the
    interval.
    """
    p = SubscriberPresence()

    cm = p.subscriber()
    cm.__enter__()  # 0→1
    try:

        async def _waiter() -> None:
            await p.wait_for_no_subscribers()

        waiter_task = asyncio.create_task(_waiter())
        await asyncio.sleep(0)
        assert not waiter_task.done()
    finally:
        cm.__exit__(None, None, None)  # 1→0

    # The drop must wake the waiter within a tick.
    await asyncio.wait_for(waiter_task, timeout=0.1)


def test_subscriber_callback_fires_on_each_zero_to_one() -> None:
    """Registered callback fires on every 0→1; not on nested entries; not on 1→0."""
    p = SubscriberPresence()
    fires: list[None] = []
    p.add_subscriber_callback(lambda: fires.append(None))

    assert fires == []

    with p.subscriber():
        assert fires == [None]
    assert fires == [None]

    with p.subscriber():
        pass
    assert fires == [None, None]

    with p.subscriber(), p.subscriber():
        pass
    assert fires == [None, None, None]


def test_subscriber_callback_isolation_protects_siblings_and_count(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A raising callback is logged; siblings still fire and the gate accounting still resets."""
    p = SubscriberPresence()

    def _bad() -> None:
        raise RuntimeError("boom")

    fires: list[None] = []
    p.add_subscriber_callback(_bad)
    p.add_subscriber_callback(lambda: fires.append(None))

    with caplog.at_level("ERROR"), p.subscriber():
        pass

    assert fires == [None]
    assert p.count == 0
    assert p._has_subscriber.is_set() is False
    assert any("subscriber callback raised" in rec.message for rec in caplog.records)
