"""Per-device live-state store, RAM-canonical with a debounced disk write.

Owns ``data_dir/.device-builder-devices.json``. Per-flavor by
design: ``/data`` is per-instance on the HA addon while
``/config/esphome`` is shared, so live mDNS observations and
per-instance build caches must NOT cross flavors. Identity stays
in the shared ``config_dir/.device-builder.json`` sidecar.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ...helpers.json import JSONDecodeError, dumps_indent, loads
from ...helpers.storage import ShutdownRegister, Store
from ..config import metadata_transaction

_LOGGER = logging.getLogger(__name__)

_STORE_FILENAME = ".device-builder-devices.json"
_SHARED_SIDECAR_FILENAME = ".device-builder.json"

# Tuned so a 70-device fleet's startup mDNS burst coalesces into
# one write per device once the broadcasts settle.
_DEFAULT_SAVE_DELAY = 2.0

# Fields the store owns. Everything else is on the shared sidecar
# (identity + ``mac_address`` + ``labels`` + top-level catalogs).
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

        Flush new file first, strip the shared sidecar second.
        A crash between the two leaves both files with the same
        per-device data; next ``async_load`` reads the new file
        and skips migration.
        """
        loaded = await self._store.async_load()
        if loaded is not None:
            self._state = loaded
            return
        loop = asyncio.get_running_loop()
        migrated = await loop.run_in_executor(None, self._migrate_read_shared_sync)
        self._state = migrated
        if not migrated:
            return
        self._store.async_delay_save(self._snapshot, delay=0.0)
        await self._store.async_save_now()
        keys = list(migrated.keys())
        await loop.run_in_executor(None, self._migrate_strip_shared_sync, keys)
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
        """Merge *fields* into *filename*; schedule debounced flush.

        Tri-state: ``None`` leaves the field alone, truthy writes,
        falsy clears the key (and drops the entry when the last
        key clears). Callers persisting a meaningful falsy value
        (``api_encryption_active=""``) use :meth:`set_field`.
        """
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
        """Write *key=value* literally; bypass :meth:`update`'s tri-state.

        For values whose falsy form is the truth that needs
        persisting (``api_encryption_active=""`` for plaintext-
        confirmed, distinct from ``None`` for unobserved).
        """
        new_entry = {**self._state.get(filename, {}), key: value}
        self._commit_entry(filename, new_entry, delay=delay)

    async def remove(self, filename: str) -> None:
        """Drop *filename*'s entry and flush immediately."""
        if self._commit_entry(filename, {}, delay=0.0):
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
        """Replace *filename*'s entry; schedule a save iff anything changed.

        Drops the entry entirely when *new_entry* is empty.
        Always builds a fresh dict on update so executor-thread
        :meth:`get` reads can't observe a half-mutated entry.
        Returns True when state changed, False on no-op.
        """
        if new_entry == self._state.get(filename, {}):
            return False
        if new_entry:
            self._state[filename] = new_entry
        else:
            self._state.pop(filename, None)
        self._store.async_delay_save(self._snapshot, delay=delay)
        return True

    def _snapshot(self) -> dict[str, dict[str, Any]]:
        """Return a top-level copy of the RAM dict for ``Store`` to encode.

        ``Store`` invokes this on the event loop and hands the
        result to the executor for encoding. A live ref would let
        the executor iterate while a concurrent ``_commit_entry``
        adds a key (``RuntimeError: dictionary changed size``).
        Inner dicts can stay shared: every mutator replaces the
        per-filename dict rather than mutating it in place, so
        the encoder's view of any single entry is stable.
        """
        return dict(self._state)

    def _migrate_read_shared_sync(self) -> dict[str, dict[str, Any]]:
        """Read store-shaped fields out of the shared sidecar (no mutation)."""
        shared_path = self._config_dir / _SHARED_SIDECAR_FILENAME
        if not shared_path.exists():
            return {}
        migrated: dict[str, dict[str, Any]] = {}
        with metadata_transaction(self._config_dir) as data:
            for key, value in data.items():
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
