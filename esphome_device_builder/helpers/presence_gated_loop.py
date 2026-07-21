"""Presence-gated periodic loop: park while no dashboard client is subscribed."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .subscriber_presence import SubscriberPresence

_LOGGER = logging.getLogger(__name__)


class PresenceGatedLoop[WorkT]:
    """
    Presence-gated fixed-interval loop; subclasses supply ``_work``.

    Tick anatomy: gate → resume hook (real park only) → wake-clear →
    work → idle → after-idle hook with that tick's ``_work`` result
    (skipped when ``_work`` crashed, so the hook never sees stale state).
    """

    # Names the loop in the crash-continue log line.
    _label: str
    # Head start before the first tick.
    _bootstrap_delay: float = 0
    # Seconds between ticks.
    _interval: float = 60
    # True logs a crashing ``_work`` and keeps looping (process-lifetime
    # loops); False propagates so the owning supervisor tears down
    # (per-session loops).
    _continue_on_error: bool = True

    def __init__(self, presence: SubscriberPresence | None) -> None:
        self._presence = presence
        # Cleared at the top of each iteration so a wake fired
        # mid-work still triggers the next idle. The presence 0→1
        # transition is multiplexed into the same event so a
        # subscriber arriving mid-idle doesn't wait out the interval.
        self._wake = asyncio.Event()
        self._running = False
        self._error_logged = False
        self._unsub_wake: Callable[[], None] | None = None
        if presence is not None:
            self._unsub_wake = presence.add_subscriber_callback(self._wake.set)

    def wake(self) -> None:
        """Bail the idle wait so the next tick runs without waiting out the interval."""
        self._wake.set()

    def unsubscribe(self) -> None:
        """Detach the presence wake callback (owners shorter-lived than the gate)."""
        if self._unsub_wake is not None:
            self._unsub_wake()
            self._unsub_wake = None

    async def run(self) -> None:
        # Sequential re-runs are fine (the MQTT loop re-runs per broker
        # session); a concurrent second run would silently double-tick.
        if self._running:
            msg = f"{type(self).__name__} is already running"
            raise RuntimeError(msg)
        self._running = True
        try:
            await asyncio.sleep(self._bootstrap_delay)
            if not await self._prepare():
                return
            # Strict pause when wired to a SubscriberPresence gate: only
            # work while at least one dashboard client is subscribed.
            while True:
                if self._presence is not None and not self._presence.has_subscribers():
                    await self._presence.wait_for_subscriber()
                    self._on_resume()
                self._wake.clear()
                try:
                    result = await self._work()
                except Exception:
                    if not self._continue_on_error:
                        raise
                    # A failure must not kill the loop for the process
                    # lifetime. Traceback once per failure streak, DEBUG
                    # on repeats — re-armed by the next successful tick —
                    # so a short interval can't flood the log.
                    if self._error_logged:
                        _LOGGER.debug("%s still failing; continuing", self._label, exc_info=True)
                    else:
                        _LOGGER.exception("%s failed; continuing", self._label)
                        self._error_logged = True
                    await self._idle()
                    continue
                self._error_logged = False
                await self._idle()
                self._after_idle(result)
        finally:
            self._running = False

    async def _prepare(self) -> bool:
        """One-shot gate after the bootstrap sleep; False disables the loop."""
        return True

    def _on_resume(self) -> None:
        """Run when a real park ends (presence 0→1 while this loop waited)."""

    async def _work(self) -> WorkT:
        raise NotImplementedError

    async def _idle(self) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=self._interval)

    def _after_idle(self, result: WorkT) -> None:
        """Run after each completed tick's idle with that tick's ``_work`` result."""
