"""Monotonic cooldown bookkeeping keyed on caller-defined keys."""

from __future__ import annotations

import time
from collections.abc import Callable


class CooldownLedger[K]:
    """(key → deadline) map with optional consecutive-failure escalation."""

    def __init__(self) -> None:
        self._deadline: dict[K, float] = {}
        self._strikes: dict[K, int] = {}

    def __len__(self) -> int:
        """Count of keys currently holding a deadline (expired or not)."""
        return len(self._deadline)

    def __contains__(self, key: K) -> bool:
        """Report whether *key* holds a deadline (expired or not)."""
        return key in self._deadline

    def ready(self, key: K, now: float | None = None) -> bool:
        """Report whether *key* is off cooldown; pass *now* to hoist the clock read in loops."""
        if now is None:
            now = time.monotonic()
        return self._deadline.get(key, 0.0) <= now

    def remaining(self, key: K) -> float:
        """Seconds until *key* is ready again; 0 when it already is."""
        return max(0.0, self._deadline.get(key, 0.0) - time.monotonic())

    def strikes(self, key: K) -> int:
        """Consecutive :meth:`escalate` calls recorded for *key*."""
        return self._strikes.get(key, 0)

    def set(self, key: K, seconds: float) -> None:
        """Hold *key* for *seconds* from now."""
        self._deadline[key] = time.monotonic() + seconds

    def escalate(self, key: K, base: float, cap: float) -> None:
        """Hold *key* with a delay doubling per consecutive call, capped at *cap*."""
        strikes = self._strikes.get(key, 0) + 1
        self._strikes[key] = strikes
        self.set(key, min(base * 2 ** (strikes - 1), cap))

    def discard(self, key: K) -> None:
        """Drop *key*'s deadline and strike count; no-op when absent."""
        self._deadline.pop(key, None)
        self._strikes.pop(key, None)

    def clear(self) -> None:
        """Drop every deadline and strike count."""
        self._deadline.clear()
        self._strikes.clear()

    def prune(self, keep: Callable[[K], bool]) -> None:
        """Drop every key (and its strike count) that *keep* rejects."""
        self._deadline = {k: v for k, v in self._deadline.items() if keep(k)}
        self._strikes = {k: v for k, v in self._strikes.items() if keep(k)}
