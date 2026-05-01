"""
Device YAML file scanner.

Watches the configured directory for new / changed / removed device
YAML files, materialises them into ``Device`` instances, and emits a
single change event per file via callbacks. Cache keys (inode, dev,
mtime, size) are used to avoid re-parsing files that haven't changed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from esphome import util

from ..helpers.device_yaml import load_device_from_storage
from ..models import Device

_LOGGER = logging.getLogger(__name__)

# (inode, device, mtime, size) — combined cache key for change detection.
_CacheKey = tuple[int, int, float, int]


class ScanChange(StrEnum):
    """Reasons a scan might surface a device."""

    ADDED = "added"
    UPDATED = "updated"
    REMOVED = "removed"


# Callback invoked for every detected change. Receives the kind of
# change and the affected ``Device`` model. The owner is responsible
# for firing whatever events / state updates are appropriate.
ScanCallback = Callable[[ScanChange, Device], None]


class DeviceScanner:
    """
    Disk-backed device cache.

    ``scan()`` is safe to call concurrently — overlapping calls coalesce
    via an internal lock. Use ``devices`` to read the current snapshot.
    """

    def __init__(
        self,
        config_dir: Path,
        get_board_id: Callable[[Path, str], str],
        on_change: ScanCallback,
        get_ip: Callable[[Path, str], str] | None = None,
    ) -> None:
        self._config_dir = config_dir
        self._get_board_id = get_board_id
        # Optional IP resolver — looks up the last-known resolved IP
        # from the metadata sidecar so the OTA address cache survives
        # restarts. Defaults to a no-op so tests that don't care about
        # persistence stay simple.
        self._get_ip = get_ip or (lambda _config_dir, _filename: "")
        self._on_change = on_change
        self._devices: dict[Path, Device] = {}
        self._cache_keys: dict[Path, _CacheKey] = {}
        self._lock = asyncio.Lock()

    @property
    def devices(self) -> list[Device]:
        """Snapshot of the currently-loaded devices."""
        return list(self._devices.values())

    @property
    def by_path(self) -> dict[Path, Device]:
        """Live mapping ``path → Device``. Treat as read-only."""
        return self._devices

    async def scan(self) -> None:
        """Refresh the device cache from disk, emitting per-file change events."""
        async with self._lock:
            await self._do_scan()

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
                self._devices[path] = device
                self._cache_keys[path] = path_to_cache_key[path]
                kind = ScanChange.ADDED if path in added_paths else ScanChange.UPDATED
                self._on_change(kind, device)

        for path in removed_paths:
            removed_device: Device | None = self._devices.pop(path, None)  # type: ignore[arg-type]
            self._cache_keys.pop(path, None)  # type: ignore[arg-type]
            if removed_device is not None:
                self._on_change(ScanChange.REMOVED, removed_device)

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
                board_id = self._get_board_id(self._config_dir, path.name)
                ip = self._get_ip(self._config_dir, path.name)
                result[path] = load_device_from_storage(path, board_id, ip)
            except Exception:
                _LOGGER.warning("Failed to load device from %s", path.name)
        return result
