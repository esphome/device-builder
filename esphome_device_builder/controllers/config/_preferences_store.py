"""RAM-canonical user preferences with a debounced disk write."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ...helpers.json import JSONDecodeError, dumps_indent, loads
from ...helpers.storage import ShutdownRegister, Store
from ...models import UserPreferences
from .metadata import _load_metadata, metadata_transaction
from .preferences import _PREFS_KEY, _prefs_from_data

_LOGGER = logging.getLogger(__name__)

_STORE_FILENAME = ".device-builder-preferences.json"
_SHARED_SIDECAR_FILENAME = ".device-builder.json"

_DEFAULT_SAVE_DELAY = 1.0


def _encode(prefs: UserPreferences) -> bytes:
    return dumps_indent(prefs.to_dict())


def _decode(raw: bytes) -> UserPreferences:
    try:
        obj = loads(raw)
    except JSONDecodeError:
        _LOGGER.warning("preferences store: corrupt JSON, starting from defaults")
        return UserPreferences()
    if not isinstance(obj, dict):
        _LOGGER.warning("preferences store: non-object payload, starting from defaults")
        return UserPreferences()
    try:
        return UserPreferences.from_dict(obj)
    except (ValueError, TypeError, LookupError):
        _LOGGER.exception("preferences store: undecodable payload, starting from defaults")
        return UserPreferences()


class PreferencesStore:
    """RAM-canonical user preferences; writes go through a debounced ``Store``."""

    def __init__(self, config_dir: Path, shutdown_register: ShutdownRegister) -> None:
        self._config_dir = config_dir
        self._state = UserPreferences()
        self._store: Store[UserPreferences] = Store(
            config_dir / _STORE_FILENAME,
            encoder=_encode,
            decoder=_decode,
            shutdown_register=shutdown_register,
            name="preferences",
        )

    async def async_load(self) -> None:
        """Seed RAM from disk; migrate the sidecar's ``_preferences`` on first run.

        Flushes the dedicated file before stripping the sidecar key so a crash
        between the two preserves the migration.
        """
        loaded = await self._store.async_load()
        if loaded is not None:
            self._state = loaded
            return
        loop = asyncio.get_running_loop()
        migrated = await loop.run_in_executor(None, self._migrate_read_shared_sync)
        if migrated is None:
            return
        self._state = migrated
        self._store.async_delay_save(self._snapshot, delay=0.0)
        await self._store.async_save_now()
        await loop.run_in_executor(None, self._migrate_strip_shared_sync)
        _LOGGER.info(
            "Migrated preferences from %s to %s", _SHARED_SIDECAR_FILENAME, _STORE_FILENAME
        )

    def snapshot(self) -> UserPreferences:
        """Return a copy of the current preferences (sync; for the subscribe snapshot).

        A copy so a caller mutating it can't corrupt the canonical RAM state
        (which would skip the debounced write and be lost on restart).
        """
        return self._copy()

    def update(
        self, fields: dict[str, Any], *, delay: float = _DEFAULT_SAVE_DELAY
    ) -> UserPreferences:
        """Merge a validated partial dict and schedule a debounced save."""
        new = UserPreferences.from_dict({**self._state.to_dict(), **fields})
        self._state = new
        self._store.async_delay_save(self._snapshot, delay=delay)
        return new

    def mutate(
        self,
        fn: Callable[[UserPreferences], UserPreferences | None],
        *,
        delay: float = _DEFAULT_SAVE_DELAY,
    ) -> UserPreferences:
        """Apply *fn* to a copy, replace RAM, schedule a save, return the result.

        *fn* may mutate the passed copy in place and return ``None`` (in-RAM
        state is always replaced, never mutated in place, so a borrowed
        :meth:`snapshot` reference stays stable).
        """
        working = self._copy()
        result = fn(working)
        if result is None:
            result = working
        self._state = result
        self._store.async_delay_save(self._snapshot, delay=delay)
        return result

    def _copy(self) -> UserPreferences:
        """Return a fresh, independent copy of the canonical RAM state."""
        return UserPreferences.from_dict(self._state.to_dict())

    def _snapshot(self) -> UserPreferences:
        return self._state

    def _migrate_read_shared_sync(self) -> UserPreferences | None:
        """Decode the sidecar's ``_preferences`` blob, or ``None`` if absent."""
        shared_path = self._config_dir / _SHARED_SIDECAR_FILENAME
        if not shared_path.exists():
            return None
        data = _load_metadata(self._config_dir)
        if _PREFS_KEY not in data:
            return None
        return _prefs_from_data(data)

    def _migrate_strip_shared_sync(self) -> None:
        """Drop the migrated ``_preferences`` key from the shared sidecar."""
        with metadata_transaction(self._config_dir) as data:
            data.pop(_PREFS_KEY, None)
