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
from .preferences import _PREFS_KEY

_LOGGER = logging.getLogger(__name__)

_STORE_FILENAME = ".device-builder-preferences.json"
_SHARED_SIDECAR_FILENAME = ".device-builder.json"

_DEFAULT_SAVE_DELAY = 1.0

# Decode failures that should be treated as "corrupt / incompatible": a bad-JSON
# read, a non-object payload, or a shape ``from_dict`` rejects.
_DECODE_ERRORS = (JSONDecodeError, ValueError, TypeError, LookupError)


def _encode(prefs: UserPreferences) -> bytes:
    return dumps_indent(prefs.to_dict())


def _decode(raw: bytes) -> UserPreferences:
    """Decode stored preferences; raise on a corrupt or incompatible payload.

    Corruption propagates (rather than defaulting) so the caller can preserve
    the file for recovery instead of silently overwriting it.
    """
    obj = loads(raw)
    if not isinstance(obj, dict):
        raise TypeError("preferences payload is not a JSON object")
    return UserPreferences.from_dict(obj)


class PreferencesStore:
    """RAM-canonical user preferences; writes go through a debounced ``Store``."""

    def __init__(self, config_dir: Path, shutdown_register: ShutdownRegister) -> None:
        self._config_dir = config_dir
        self._state = UserPreferences()
        # Set when an undecodable file couldn't be renamed aside; suppresses all
        # writes so a later save can't overwrite the still-recoverable corrupt file.
        self._persist_disabled = False
        self._store: Store[UserPreferences] = Store(
            config_dir / _STORE_FILENAME,
            encoder=_encode,
            decoder=_decode,
            shutdown_register=shutdown_register,
            name="preferences",
        )

    async def async_load(self) -> None:
        """Seed RAM from disk; migrate the sidecar's ``_preferences`` on first run.

        Undecodable data is preserved, never destroyed: a corrupt dedicated file
        is renamed aside (then the legacy sidecar is still tried, so a recoverable
        blob isn't lost), and an undecodable legacy blob is left in place. Both
        fall back to defaults.
        """
        loop = asyncio.get_running_loop()
        try:
            loaded = await self._store.async_load()
        except _DECODE_ERRORS:
            _LOGGER.exception(
                "preferences store: %s is undecodable; preserving it and using defaults",
                _STORE_FILENAME,
            )
            await loop.run_in_executor(None, self._preserve_corrupt_file)
            await self._migrate_from_sidecar(loop)
            return
        if loaded is not None:
            self._state = loaded
            return
        await self._migrate_from_sidecar(loop)

    async def _migrate_from_sidecar(self, loop: asyncio.AbstractEventLoop) -> None:
        """Adopt the legacy ``_preferences`` blob, persist it, then strip the key.

        The strip is gated on a confirmed write so an unconfirmed flush can't lose
        the prefs on restart (see :meth:`_confirm_and_strip_shared_sync`).
        """
        migrated = await loop.run_in_executor(None, self._migrate_read_shared_sync)
        if migrated is None:
            return
        self._state = migrated
        if self._persist_disabled:
            return
        self._store.async_delay_save(self._snapshot, delay=0.0)
        await self._store.async_save_now()
        stripped = await loop.run_in_executor(None, self._confirm_and_strip_shared_sync)
        if not stripped:
            _LOGGER.warning(
                "preferences store: %s write unconfirmed; keeping %s in %s to retry",
                _STORE_FILENAME,
                _PREFS_KEY,
                _SHARED_SIDECAR_FILENAME,
            )
            return
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
        self._state = UserPreferences.from_dict({**self._state.to_dict(), **fields})
        self._schedule_save(delay=delay)
        return self._copy()

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
        self._schedule_save(delay=delay)
        return self._copy()

    def _schedule_save(self, *, delay: float) -> None:
        """Schedule a debounced write, unless persistence has been disabled."""
        if self._persist_disabled:
            return
        self._store.async_delay_save(self._snapshot, delay=delay)

    def _copy(self) -> UserPreferences:
        """Return a fresh, independent copy of the canonical RAM state."""
        return UserPreferences.from_dict(self._state.to_dict())

    def _snapshot(self) -> UserPreferences:
        return self._state

    def _preserve_corrupt_file(self) -> None:
        """Rename the undecodable dedicated file aside so the next save can't erase it.

        If the rename fails, disable persistence: leaving the corrupt file in
        place and then writing over it would destroy the recoverable data this
        method exists to protect.
        """
        path = self._config_dir / _STORE_FILENAME
        try:
            path.replace(path.with_name(path.name + ".corrupt"))
        except OSError:
            self._persist_disabled = True
            _LOGGER.warning(
                "Could not preserve corrupt preferences file %s; disabling writes to keep it",
                path,
                exc_info=True,
            )

    def _migrate_read_shared_sync(self) -> UserPreferences | None:
        """Decode the sidecar's ``_preferences`` blob.

        Returns ``None`` when the key is absent or undecodable; an undecodable
        legacy blob is logged and left in the sidecar (the caller doesn't strip
        it) so the data stays recoverable rather than being replaced by defaults.
        """
        shared_path = self._config_dir / _SHARED_SIDECAR_FILENAME
        if not shared_path.exists():
            return None
        data = _load_metadata(self._config_dir)
        if _PREFS_KEY not in data:
            return None
        try:
            return UserPreferences.from_dict(data[_PREFS_KEY])
        except _DECODE_ERRORS:
            _LOGGER.exception(
                "preferences store: legacy _preferences blob undecodable; left in %s for recovery",
                _SHARED_SIDECAR_FILENAME,
            )
            return None

    def _confirm_and_strip_shared_sync(self) -> bool:
        """Strip the migrated ``_preferences`` key once the dedicated write landed.

        Returns ``False`` only when the dedicated-file write is unconfirmed
        (``Store`` swallows write errors), so the caller keeps the legacy key for
        a retry. A *strip* failure is non-fatal and still returns ``True``: the
        dedicated file is already canonical and a leftover legacy key is benign
        (the dedicated file wins on the next load), so it must not abort boot.
        The ``exists`` probe and the strip share one executor hop.
        """
        if not self._store.path.exists():
            return False
        try:
            with metadata_transaction(self._config_dir) as data:
                data.pop(_PREFS_KEY, None)
        except OSError:
            _LOGGER.warning(
                "preferences store: migrated prefs but could not strip %s from %s; "
                "the dedicated file wins, leaving the stale key",
                _PREFS_KEY,
                _SHARED_SIDECAR_FILENAME,
                exc_info=True,
            )
        return True
