"""Per-key ``asyncio.TimerHandle`` bookkeeping with cancel-and-replace arming."""

from __future__ import annotations

import asyncio


class TimerRegistry[K]:
    """(key → timer handle) map; arming a held key cancels the prior handle."""

    def __init__(self) -> None:
        self._timers: dict[K, asyncio.TimerHandle] = {}

    def __contains__(self, key: K) -> bool:
        """Report whether *key* holds a handle."""
        return key in self._timers

    def __len__(self) -> int:
        """Count of keys currently holding a handle."""
        return len(self._timers)

    def __getitem__(self, key: K) -> asyncio.TimerHandle:
        """Return *key*'s handle; KeyError when absent."""
        return self._timers[key]

    def arm(self, key: K, handle: asyncio.TimerHandle) -> None:
        """Install *handle* for *key*, cancelling any prior one."""
        existing = self._timers.pop(key, None)
        if existing is not None:
            existing.cancel()
        self._timers[key] = handle

    def discard(self, key: K) -> None:
        """Cancel and drop *key*'s handle, if any (cancelling a fired handle is inert)."""
        handle = self._timers.pop(key, None)
        if handle is not None:
            handle.cancel()

    def cancel_all(self) -> list[K]:
        """Cancel every handle and clear; return the keys that were held."""
        keys = list(self._timers)
        for handle in self._timers.values():
            handle.cancel()
        self._timers.clear()
        return keys
