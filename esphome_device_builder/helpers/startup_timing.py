"""Monotonic startup phase timer; cross-platform, no OS-specific calls."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

__all__ = ["StartupTimer"]

_LOGGER = logging.getLogger("esphome_device_builder.startup")


class StartupTimer:
    """Accumulates named startup phase durations off a monotonic origin."""

    def __init__(self, origin: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._origin = origin
        self._last = origin
        self._phases: list[tuple[str, float]] = []

    def mark(self, name: str) -> float:
        """Record the delta since the previous mark and return it (seconds)."""
        now = self._clock()
        delta = now - self._last
        self._last = now
        self._phases.append((name, delta))
        _LOGGER.debug("startup phase %s: %.3fs", name, delta)
        return delta

    @property
    def total(self) -> float:
        """Seconds from the origin to the last mark."""
        return self._last - self._origin

    def summary(self) -> str:
        """One-line breakdown, e.g. ``total=12.7s (import=9.4s app=0.1s)``."""
        # Total is the sum of the rounded parts so the line is self-consistent.
        rounded = [(name, round(delta, 1)) for name, delta in self._phases]
        total = sum(delta for _, delta in rounded)
        phases = " ".join(f"{name}={delta:.1f}s" for name, delta in rounded)
        return f"total={total:.1f}s ({phases})"
