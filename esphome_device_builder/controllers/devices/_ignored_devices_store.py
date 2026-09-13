"""The dashboard's ignored-devices list; the file is shared with the legacy dashboard."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ...helpers.json import JSONDecodeError, dumps_indent, loads
from ...helpers.storage import Store

if TYPE_CHECKING:
    from pathlib import Path

    from ...helpers.storage import ShutdownRegister

_LOGGER = logging.getLogger(__name__)

_SAVE_DELAY = 1.0


def _encode(names: set[str]) -> bytes:
    return dumps_indent({"ignored_devices": sorted(names)})


def _decode(raw: bytes) -> set[str]:
    try:
        obj = loads(raw)
    except JSONDecodeError:
        _LOGGER.warning("ignored devices store: corrupt JSON, starting empty")
        return set()
    if not isinstance(obj, dict):
        _LOGGER.warning("ignored devices store: non-mapping JSON, starting empty")
        return set()
    names = obj.get("ignored_devices", [])
    if not isinstance(names, list):
        _LOGGER.warning("ignored devices store: non-list ``ignored_devices`` field, starting empty")
        return set()
    return {name for name in names if isinstance(name, str)}


class IgnoredDevicesStore:
    """RAM-canonical ignored names, mutated in place; writes go through a debounced ``Store``."""

    def __init__(self, path: Path, names: set[str], shutdown_register: ShutdownRegister) -> None:
        self._names = names
        self._store: Store[set[str]] = Store(
            path,
            encoder=_encode,
            decoder=_decode,
            shutdown_register=shutdown_register,
            name="ignored_devices",
            mode=None,
        )

    async def async_load(self) -> None:
        """Seed the set from disk, in place."""
        loaded = await self._store.async_load()
        if loaded is not None:
            self._names.clear()
            self._names.update(loaded)

    def save(self) -> None:
        """Schedule a debounced write of the current set."""
        self._store.async_delay_save(self._snapshot, delay=_SAVE_DELAY)

    async def async_save_now(self) -> None:
        """Flush a scheduled write."""
        await self._store.async_save_now()

    def _snapshot(self) -> set[str]:
        return set(self._names)
