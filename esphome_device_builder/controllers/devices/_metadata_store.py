"""Per-device live-state store, RAM-canonical with a debounced disk write."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ...helpers.async_ import run_in_executor
from ...helpers.json import JSONDecodeError, dumps_indent, loads
from ...helpers.storage import ShutdownRegister, Store
from ..config import _load_metadata, metadata_transaction

_LOGGER = logging.getLogger(__name__)

_STORE_FILENAME = ".device-builder-devices.json"
_SHARED_SIDECAR_FILENAME = ".device-builder.json"

_DEFAULT_SAVE_DELAY = 2.0

# Fields the store owns. Everything else lives in the shared sidecar.
STORE_FIELDS: frozenset[str] = frozenset(
    {
        "ip",
        "deployed_config_hash",
        "deployed_version",
        "api_encryption_active",
        "expected_config_hash",
        "build_size_bytes",
        "build_size_dir_mtime",
        "build_size_info_mtime",
        "regen_failed_mtime",
        "regen_failed_at",
    }
)


def _encode(data: dict[str, dict[str, Any]]) -> bytes:
    return dumps_indent(data)


def _decode(raw: bytes) -> dict[str, dict[str, Any]]:
    try:
        obj = loads(raw)
    except JSONDecodeError:
        _LOGGER.warning("device metadata store: corrupt JSON, starting empty")
        return {}
    if not isinstance(obj, dict):
        return {}
    return {k: v for k, v in obj.items() if isinstance(k, str) and isinstance(v, dict)}


class DeviceMetadataStore:
    """RAM-canonical per-device live state; writes go through a debounced ``Store``."""

    def __init__(
        self,
        config_dir: Path,
        data_dir: Path,
        shutdown_register: ShutdownRegister,
    ) -> None:
        self._config_dir = config_dir
        self._state: dict[str, dict[str, Any]] = {}
        self._store: Store[dict[str, dict[str, Any]]] = Store(
            data_dir / _STORE_FILENAME,
            encoder=_encode,
            decoder=_decode,
            shutdown_register=shutdown_register,
            name="device_metadata",
        )

    async def async_load(self) -> None:
        """Seed RAM from disk; migrate from the shared sidecar on first run.

        Flushes the new file before stripping the shared sidecar
        so a crash between the two preserves the migration.
        """
        loaded = await self._store.async_load()
        if loaded is not None:
            self._state = loaded
            return
        migrated = await run_in_executor(self._migrate_read_shared_sync)
        self._state = migrated
        if not migrated:
            return
        self._store.async_delay_save(self._snapshot, delay=0.0)
        await self._store.async_save_now()
        keys = list(migrated.keys())
        await run_in_executor(self._migrate_strip_shared_sync, keys)
        _LOGGER.info(
            "Migrated %d device metadata entries from %s to %s",
            len(migrated),
            _SHARED_SIDECAR_FILENAME,
            _STORE_FILENAME,
        )

    def get(self, filename: str) -> dict[str, Any]:
        """Return a shallow copy of *filename*'s metadata."""
        return dict(self._state.get(filename, {}))

    def update(
        self,
        filename: str,
        *,
        delay: float = _DEFAULT_SAVE_DELAY,
        **fields: Any,
    ) -> None:
        """Merge *fields* into *filename*; tri-state (None=leave, truthy=write, falsy=clear)."""
        new_entry = dict(self._state.get(filename, {}))
        for key, value in fields.items():
            if value is None:
                continue
            if value:
                new_entry[key] = value
            else:
                new_entry.pop(key, None)
        self._commit_entry(filename, new_entry, delay=delay)

    def set_field(
        self,
        filename: str,
        key: str,
        value: Any,
        *,
        delay: float = _DEFAULT_SAVE_DELAY,
    ) -> None:
        """Write *key=value* literally; bypass :meth:`update`'s tri-state semantics."""
        new_entry = {**self._state.get(filename, {}), key: value}
        self._commit_entry(filename, new_entry, delay=delay)

    async def remove(self, filename: str) -> None:
        """Drop *filename*'s entry and flush immediately."""
        if self._commit_entry(filename, {}, delay=0.0):
            await self._store.async_save_now()

    async def rename(self, old_filename: str, new_filename: str) -> None:
        """Move *old_filename*'s entry to *new_filename*; flush immediately.

        Pre-existing *new_filename* fields win on conflict, mirroring
        the shared sidecar's rename so the two stores agree.
        """
        if old_filename == new_filename:
            return
        old_entry = self._state.get(old_filename)
        if not old_entry:
            return
        merged = {**old_entry, **self._state.get(new_filename, {})}
        self._commit_rename(old_filename, new_filename, merged)
        await self._store.async_save_now()

    def clear_volatile(self, filename: str) -> None:
        """Drop every store-owned field for *filename*."""
        current = self._state.get(filename)
        if current is None:
            return
        new_entry = {k: v for k, v in current.items() if k not in STORE_FIELDS}
        self._commit_entry(filename, new_entry, delay=_DEFAULT_SAVE_DELAY)

    def snapshot_all(self) -> dict[str, dict[str, Any]]:
        """Return a defensive copy of the full RAM state."""
        return {k: dict(v) for k, v in self._state.items()}

    def _commit_entry(self, filename: str, new_entry: dict[str, Any], *, delay: float) -> bool:
        """Replace *filename*'s entry; returns True iff state changed.

        Mutators MUST replace the inner dict (never
        ``self._state[filename][key] = ...`` in place): scanner
        executor threads read entries through :meth:`get` and the
        encoder reads them through :meth:`_snapshot`, both of which
        rely on the inner dict being immutable for the lifetime of
        any borrowed reference.
        """
        if new_entry == self._state.get(filename, {}):
            return False
        if new_entry:
            self._state[filename] = new_entry
        else:
            self._state.pop(filename, None)
        self._store.async_delay_save(self._snapshot, delay=delay)
        return True

    def _commit_rename(self, old_filename: str, new_filename: str, merged: dict[str, Any]) -> None:
        """Drop *old_filename* and land *merged* on *new_filename* in one step.

        Both dict mutations run before the single ``async_delay_save``
        so no save snapshot ever observes the half-renamed state (an
        executor encode racing a second ``_commit_entry`` could see
        both keys or trip ``dict changed size during iteration``).
        Callers gate on a truthy *merged* and a present *old_filename*.
        """
        self._state[new_filename] = merged
        self._state.pop(old_filename, None)
        self._store.async_delay_save(self._snapshot, delay=0.0)

    def _snapshot(self) -> dict[str, dict[str, Any]]:
        """Top-level copy of the RAM dict for ``Store``'s executor-thread encode.

        Inner dicts stay shared because :meth:`_commit_entry`
        replaces rather than mutating in place.
        """
        return dict(self._state)

    def _migrate_read_shared_sync(self) -> dict[str, dict[str, Any]]:
        """Read store-shaped fields out of the shared sidecar.

        Plain ``_load_metadata`` (not ``metadata_transaction``)
        because the transaction unconditionally re-saves on exit
        — pointless for a read-only scan that often finds nothing
        to migrate. ``_save_metadata`` uses atomic rename so the
        read sees a consistent snapshot without the flock.
        """
        shared_path = self._config_dir / _SHARED_SIDECAR_FILENAME
        if not shared_path.exists():
            return {}
        migrated: dict[str, dict[str, Any]] = {}
        for key, value in _load_metadata(self._config_dir).items():
            # Top-level catalogs use ``_``-prefixed keys.
            if key.startswith("_"):
                continue
            if not isinstance(value, dict):
                continue
            store_fields = {k: v for k, v in value.items() if k in STORE_FIELDS}
            if store_fields:
                migrated[key] = store_fields
        return migrated

    def _migrate_strip_shared_sync(self, keys: Iterable[str]) -> None:
        """Pop store-owned fields from each shared-sidecar entry."""
        with metadata_transaction(self._config_dir) as data:
            for key in keys:
                entry = data.get(key)
                if not isinstance(entry, dict):
                    continue
                for field_name in list(entry):
                    if field_name in STORE_FIELDS:
                        entry.pop(field_name, None)
                if not entry:
                    data.pop(key, None)
