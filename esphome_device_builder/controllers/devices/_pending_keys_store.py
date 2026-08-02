"""Name-keyed store of HA-provisioned API keys awaiting adoption."""

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING

from ...helpers.json import JSONDecodeError, dumps_indent, loads
from ...helpers.storage import Store

if TYPE_CHECKING:
    from pathlib import Path

    from ...helpers.storage import ShutdownRegister

_LOGGER = logging.getLogger(__name__)

_STORE_FILENAME = ".device-builder-pending-keys.json"
_SAVE_DELAY = 1.0


def _encode(data: dict[str, dict[str, str]]) -> bytes:
    return dumps_indent(data)


def _decode(raw: bytes) -> dict[str, dict[str, str]]:
    try:
        obj = loads(raw)
    except JSONDecodeError:
        _LOGGER.warning("pending keys store: corrupt JSON, starting empty")
        return {}
    if not isinstance(obj, dict):
        _LOGGER.warning("pending keys store: non-mapping JSON, starting empty")
        return {}
    decoded = {k: v for k, v in obj.items() if isinstance(k, str) and _valid_entry(v)}
    if dropped := len(obj) - len(decoded):
        _LOGGER.warning("pending keys store: dropped %d malformed entries", dropped)
    return decoded


def _valid_entry(entry: object) -> bool:
    """Entry shape guard: consumers index ``entry["key"]`` unconditionally.

    The key must be base64 of exactly 32 bytes — consumers interpolate
    it into generated YAML, so nothing else may enter RAM.
    """
    if not (
        isinstance(entry, dict)
        and isinstance(entry.get("key"), str)
        and all(isinstance(v, str) for v in entry.values())
    ):
        return False
    try:
        return len(base64.b64decode(entry["key"], validate=True)) == 32
    except ValueError:
        return False


class PendingKeysStore:
    """RAM-canonical pending keys; writes go through a debounced ``Store``."""

    def __init__(self, data_dir: Path, shutdown_register: ShutdownRegister) -> None:
        self._state: dict[str, dict[str, str]] = {}
        self._store: Store[dict[str, dict[str, str]]] = Store(
            data_dir / _STORE_FILENAME,
            encoder=_encode,
            decoder=_decode,
            shutdown_register=shutdown_register,
            name="pending_keys",
        )

    async def async_load(self) -> None:
        """Seed RAM from disk."""
        loaded = await self._store.async_load()
        if loaded is not None:
            self._state = loaded

    def get(self, name: str) -> dict[str, str] | None:
        """Return a copy of *name*'s pending entry, or ``None``."""
        entry = self._state.get(name)
        return dict(entry) if entry is not None else None

    def set(self, name: str, key: str, mac: str = "") -> None:
        """Store or overwrite the pending key for *name*."""
        entry = {"key": key}
        if mac:
            entry["mac"] = mac
        if self._state.get(name) == entry:
            return
        self._state[name] = entry
        self._store.async_delay_save(self._snapshot, delay=_SAVE_DELAY)

    def pop(self, name: str) -> dict[str, str] | None:
        """Drop and return *name*'s pending entry, or ``None``."""
        entry = self._state.pop(name, None)
        if entry is not None:
            self._store.async_delay_save(self._snapshot, delay=_SAVE_DELAY)
        return entry

    def _snapshot(self) -> dict[str, dict[str, str]]:
        return dict(self._state)
