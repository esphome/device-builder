"""
Device YAML file scanner.

Watches the configured directory for new / changed / removed device
YAML files, materialises them into ``Device`` instances, and emits a
single change event per file via callbacks. Cache keys (inode, dev,
mtime, size) are used to avoid re-parsing files that haven't changed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple

from esphome import util

from ..helpers.device_yaml import load_device_from_storage
from ..models import Device

_LOGGER = logging.getLogger(__name__)

# (inode, device, mtime, size) — combined cache key for change detection.
_CacheKey = tuple[int, int, float, int]


class DeviceFileMetadata(NamedTuple):
    """Persisted sidecar fields the scanner threads into each ``Device``."""

    board_id: str
    ip: str
    expected_config_hash: str = ""


class ScanChange(StrEnum):
    """Reasons a scan might surface a device."""

    ADDED = "added"
    UPDATED = "updated"
    REMOVED = "removed"


# Callback invoked for every detected change. Receives the kind of
# change and the affected ``Device`` model. The owner is responsible
# for firing whatever events / state updates are appropriate.
ScanCallback = Callable[[ScanChange, Device], None]

# Callback that resolves the persisted sidecar metadata for a device
# file. Called once per (added or updated) file during a scan.
MetadataResolver = Callable[[Path, str], DeviceFileMetadata]


class DeviceScanner:
    """
    Disk-backed device cache.

    ``scan()`` is safe to call concurrently — overlapping calls coalesce
    via an internal lock. Use ``devices`` to read the current snapshot.
    """

    def __init__(
        self,
        config_dir: Path,
        get_metadata: MetadataResolver,
        on_change: ScanCallback,
    ) -> None:
        self._config_dir = config_dir
        self._get_metadata = get_metadata
        self._on_change = on_change
        self._devices: dict[Path, Device] = {}
        # Name-keyed shadow index; mDNS / ping / MQTT observations
        # arrive keyed by the device's ``esphome.name`` and need an
        # O(1) lookup instead of an O(N) linear scan of every
        # configured YAML on every announcement. The list is by
        # design — two YAMLs can share a ``name:`` (a config plus a
        # ``foo (1).yaml`` copy, dashboard_import siblings, etc.)
        # and a single broadcast must fan out to all of them.
        self._devices_by_name: dict[str, list[Device]] = {}
        self._cache_keys: dict[Path, _CacheKey] = {}
        self._lock = asyncio.Lock()

    @property
    def devices(self) -> list[Device]:
        """Snapshot of the currently-loaded devices in lexicographic order.

        ``_devices`` is kept in sorted insertion order by ``_do_scan`` so
        this read stays O(n).
        """
        return list(self._devices.values())

    @property
    def by_path(self) -> dict[Path, Device]:
        """Live mapping ``path → Device``. Treat as read-only."""
        return self._devices

    def get_by_name(self, name: str) -> list[Device]:
        """Every configured device whose ``esphome.name`` equals *name*.

        Returns a fresh list (snapshot) — same shape as the
        ``devices`` property. Callers can iterate / mutate the
        return value without corrupting the scanner's internal
        index, and the bucket order is the lexicographic
        configuration-filename order maintained by ``_set_device``.
        """
        bucket = self._devices_by_name.get(name)
        return list(bucket) if bucket else []

    async def scan(self) -> None:
        """Refresh the device cache from disk, emitting per-file change events."""
        async with self._lock:
            await self._do_scan()

    async def reload(self, filename: str) -> bool:
        """
        Force-reload a single device's state from disk.

        Use when something other than the YAML changed but still
        affects the device model — most importantly, a successful
        firmware compile updates the binary's mtime and flips
        ``has_pending_changes``, but the YAML stat is unchanged so the
        cache-key check in :meth:`scan` would otherwise skip the
        reload.

        Returns True when the device exists and was re-read; False if
        the file isn't tracked. Fires ``ScanChange.UPDATED`` on
        success.
        """
        async with self._lock:
            path = next((p for p in self._devices if p.name == filename), None)
            if path is None:
                return False
            loop = asyncio.get_running_loop()
            loaded = await loop.run_in_executor(None, self._load_devices, {path})
            device = loaded.get(path)
            if device is None:
                return False
            self._set_device(path, device)
            try:
                stat = await loop.run_in_executor(None, path.stat)
                self._cache_keys[path] = (stat.st_ino, stat.st_dev, stat.st_mtime, stat.st_size)
            except OSError:
                pass
            self._on_change(ScanChange.UPDATED, device)
            return True

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _do_scan(self) -> None:
        loop = asyncio.get_running_loop()
        path_to_cache_key = await loop.run_in_executor(None, self._build_cache_keys)

        old_paths = set(self._devices.keys())
        new_paths = set(path_to_cache_key.keys())

        removed_paths = old_paths - new_paths
        added_paths = new_paths - old_paths
        possibly_updated = old_paths & new_paths
        updated_paths = {
            p for p in possibly_updated if path_to_cache_key[p] != self._cache_keys.get(p)
        }

        paths_to_load = added_paths | updated_paths
        if paths_to_load:
            loaded = await loop.run_in_executor(None, self._load_devices, paths_to_load)
            for path, device in loaded.items():
                self._set_device(path, device)
                self._cache_keys[path] = path_to_cache_key[path]
                kind = ScanChange.ADDED if path in added_paths else ScanChange.UPDATED
                self._on_change(kind, device)

        for path in removed_paths:
            removed_device = self._pop_device(path)
            self._cache_keys.pop(path, None)  # type: ignore[arg-type]
            if removed_device is not None:
                self._on_change(ScanChange.REMOVED, removed_device)

        # Rebuild ``_devices`` in lexicographic-path order. Without
        # this, ``paths_to_load`` is a set so ``_devices`` ends up in
        # hash-randomised insertion order — visible to the user as a
        # different device list every restart. Cheap (one dict
        # comprehension), keeps the ``devices`` property O(n). Filter
        # to paths actually present so a YAML that failed to load
        # (caught + skipped in ``_load_devices``) doesn't trigger a
        # ``KeyError`` here. ``_devices_by_name`` doesn't need a
        # parallel re-sort — its values are name-keyed lists whose
        # iteration order isn't user-visible.
        if added_paths or removed_paths:
            self._devices = {p: self._devices[p] for p in path_to_cache_key if p in self._devices}
            self._cache_keys = {
                p: self._cache_keys[p] for p in path_to_cache_key if p in self._cache_keys
            }

    def _set_device(self, path: Path, device: Device) -> None:
        """Insert / update *device* and keep ``_devices_by_name`` in lockstep.

        Buckets are sorted by ``configuration`` filename so
        ``get_by_name`` (and downstream ``bucket[0]`` consumers like
        ``_find_device_by_name``) see a deterministic order. Without
        this, the order depends on ``paths_to_load`` set iteration
        and can flip between scans, leaking spurious "first match"
        flips through the apply / dedupe path.
        """
        previous = self._devices.get(path)
        if previous is not None and previous.name != device.name:
            # Renamed in YAML: drop from old name's bucket before
            # re-inserting under the new one.
            self._unindex_name(previous)
        self._devices[path] = device
        bucket = self._devices_by_name.setdefault(device.name, [])
        if previous is not None:
            with contextlib.suppress(ValueError):
                bucket.remove(previous)
        # Insert at the position that keeps the bucket sorted by
        # configuration filename.
        insert_at = 0
        while insert_at < len(bucket) and bucket[insert_at].configuration < device.configuration:
            insert_at += 1
        if insert_at < len(bucket) and bucket[insert_at].configuration == device.configuration:
            bucket[insert_at] = device  # same path, refreshed Device
        else:
            bucket.insert(insert_at, device)

    def _pop_device(self, path: Path) -> Device | None:
        """Drop the *path* entry, mirroring the removal in ``_devices_by_name``."""
        device = self._devices.pop(path, None)
        if device is not None:
            self._unindex_name(device)
        return device

    def _unindex_name(self, device: Device) -> None:
        bucket = self._devices_by_name.get(device.name)
        if bucket is None:
            return
        try:
            bucket.remove(device)
        except ValueError:
            return
        if not bucket:
            del self._devices_by_name[device.name]

    def _build_cache_keys(self) -> dict[Path, _CacheKey]:
        """Build ``path → cache_key`` for every YAML file currently on disk."""
        result: dict[Path, _CacheKey] = {}
        for file in util.list_yaml_files([self._config_dir]):
            try:
                stat = file.stat()
            except OSError:
                continue
            result[file] = (stat.st_ino, stat.st_dev, stat.st_mtime, stat.st_size)
        return result

    def _load_devices(self, paths: set[Path]) -> dict[Path, Device]:
        """Materialise Device models for *paths*. Logs and skips on failure."""
        result: dict[Path, Device] = {}
        for path in paths:
            try:
                metadata = self._get_metadata(self._config_dir, path.name)
                result[path] = load_device_from_storage(
                    path,
                    metadata.board_id,
                    metadata.ip,
                    metadata.expected_config_hash,
                    previous=self._devices.get(path),
                )
            except Exception:
                _LOGGER.warning("Failed to load device from %s", path.name)
        return result
