"""Presence-gated fixed-interval sweep-loop base for the monitor's sources."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .controller import DeviceStateMonitor

_LOGGER = logging.getLogger(__name__)


class SweepSource:
    """Presence-gated fixed-interval sweep loop; subclasses supply ``_sweep``."""

    # Names the source in the crash-continue log line.
    _sweep_label: str
    # Head start for the other sources (e.g. the mDNS browser) so the
    # common case never reaches this source's heavier work.
    _bootstrap_delay: float
    # Seconds between sweeps.
    _interval: float = 60

    def __init__(self, monitor: DeviceStateMonitor) -> None:
        self._monitor = monitor
        # Cleared at the top of each iteration so a wake fired
        # mid-sweep still triggers the next idle. The presence 0→1
        # transition is multiplexed into the same event so a
        # subscriber arriving mid-idle doesn't wait out the interval.
        self._wake = asyncio.Event()
        if monitor._presence is not None:
            monitor._presence.add_subscriber_callback(self._wake.set)

    def wake(self) -> None:
        """Bail the idle wait so the next sweep runs without waiting out the interval."""
        self._wake.set()

    async def run(self) -> None:
        await asyncio.sleep(self._bootstrap_delay)
        if not await self._prepare():
            return
        monitor = self._monitor
        # Strict pause when wired to a SubscriberPresence gate: only
        # sweep while at least one dashboard client is subscribed.
        while True:
            if monitor._presence is not None:
                await monitor._presence.wait_for_subscriber()
            self._wake.clear()
            try:
                await self._sweep()
            except Exception:
                # A sweep failure must not kill the loop for the
                # process lifetime; log it and try again next interval.
                _LOGGER.exception("%s sweep failed; continuing", self._sweep_label)
            await self._idle()

    async def _prepare(self) -> bool:
        """One-shot gate after the bootstrap sleep; False disables the source."""
        return True

    async def _sweep(self) -> None:
        raise NotImplementedError

    async def _idle(self) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=self._interval)
