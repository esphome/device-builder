"""mDNS-record wake trigger for the peer-link client's reconnect wait."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
from typing import Any

from zeroconf import RecordUpdateListener, Zeroconf
from zeroconf.const import _TYPE_A, _TYPE_AAAA


def _mdns_record_name(hostname: str) -> str | None:
    """Return the lowercase trailing-dot record name, or ``None`` for an IP."""
    bare = hostname.rstrip(".")
    with contextlib.suppress(ValueError):
        ipaddress.ip_address(bare)
        return None
    return f"{bare.lower()}."


class _ReceiverWakeListener(RecordUpdateListener):
    """One-shot wake on an A/AAAA record for the receiver's hostname."""

    def __init__(self, record_name: str, wake: asyncio.Event) -> None:
        self._record_name = record_name
        self._wake = wake

    def async_update_records(self, zc: Zeroconf, now: float, records: list[Any]) -> None:
        if self._wake.is_set():
            return
        for update in records:
            new = update.new
            if new.type in (_TYPE_A, _TYPE_AAAA) and new.name.lower() == self._record_name:
                self._wake.set()
                return
