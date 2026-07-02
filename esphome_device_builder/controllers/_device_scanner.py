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
from collections.abc import Callable, Iterable, Mapping
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import NamedTuple

from esphome import util

from ..helpers.async_ import run_in_executor
from ..helpers.device_yaml import load_device_from_storage
from ..models import Device
from ._wake_worker import WakeWorker

_LOGGER = logging.getLogger(__name__)

# (inode, device, mtime, size) — combined cache key for change detection.
_CacheKey = tuple[int, int, float, int]


class DeviceFileMetadata(NamedTuple):
    """Persisted sidecar fields the scanner threads into each ``Device``."""

    board_id: str
    ip: str
    expected_config_hash: str = ""
    mac_address: str = ""
    build_size_bytes: int = 0
    # Tuple (immutable) of label IDs assigned to the device. The
    # global label catalog lives at ``_labels`` in the metadata
    # sidecar; this carries the per-device assignments.
    labels: tuple[str, ...] = ()
    # Last-known mDNS-broadcast config hash. Persisted so the
    # drawer's "pending changes" indicator is correct on cold load
    # without waiting for the first mDNS sweep; corrected by the
    # next ``apply_config_hash`` if the firmware was reflashed
    # externally while the dashboard was down.
    deployed_config_hash: str = ""
    # Last-known mDNS-broadcast ESPHome version. Persisted so the
    # drawer's "update available" badge renders immediately on cold
    # load instead of waiting for the first mDNS sweep.
    deployed_version: str = ""
    # Last-known mDNS api_encryption value. Truthy cipher string
    # means encryption confirmed; ``""`` means TXT seen but key
    # absent (plaintext-confirmed); ``None`` means not yet
    # broadcast.
    api_encryption_active: str | None = None


class ScanChange(StrEnum):
    """Reasons a scan might surface a device."""

    ADDED = "added"
    UPDATED = "updated"
    REMOVED = "removed"
    # A forced re-read with no YAML cache-key change (binary mtime,
    # sidecar, StorageJSON). Distinct from UPDATED so consumers that only
    # care about on-disk YAML edits (version history) can skip it.
    RELOADED = "reloaded"


# Callback invoked for every detected change. Receives the kind of
# change and the affected ``Device`` model. The owner is responsible
# for firing whatever events / state updates are appropriate.
ScanCallback = Callable[[ScanChange, Device], None]

# Callback that resolves the persisted sidecar metadata for a device
# file. Called once per (added or updated) file during a scan.
MetadataResolver = Callable[[Path, str], DeviceFileMetadata]


class _DeviceIndex:
    """
    Path-keyed Device store with lockstep name / configuration / cache-key shadows.

    The four internal maps (``_devices``, ``_devices_by_name``,
    ``_devices_by_configuration``, ``_cache_keys``) are kept in sync as a
    structural property of this class. :meth:`set` / :meth:`pop` are the
    only entry points that add or remove entries, and they update all four
    together; :meth:`rebuild_in_path_order` re-keys ``_devices`` /
    ``_cache_keys`` for iteration order only, leaving the name /
    configuration shadows' (still-valid) entries in place. Bypassing
    requires reaching into the underscore-prefixed attributes, which is
    exactly what the encapsulation is meant to discourage.

    The lockstep matters because the scanner's apply / dedupe path
    fans out an mDNS announcement to every Device sharing a
    ``name`` — the name index is what makes that fan-out O(1)
    instead of an O(N) linear scan over every configured YAML on
    every announcement. Buckets are sorted by ``configuration``
    filename so ``bucket[0]`` consumers (e.g. ``apply()``'s "first
    match" check) see a deterministic order across scans;
    set-derived iteration would otherwise let the dedupe flip-flop
    across scans for duplicate-named YAMLs.

    The ``cache_keys`` map carries (inode, dev, mtime, size) per
    path so :class:`DeviceScanner` can short-circuit re-parsing
    files that haven't changed. It's coupled to the device
    lifecycle (added/removed together) so it lives here too —
    keeping it in a separate dict on the scanner risks a leak if a
    future caller pops a device without also popping its key.
    """

    def __init__(self) -> None:
        self._devices: dict[Path, Device] = {}
        # Name-keyed shadow index; mDNS / ping / MQTT observations
        # arrive keyed by the device's ``esphome.name`` and need an
        # O(1) lookup instead of an O(N) linear scan of every
        # configured YAML on every announcement. The list is by
        # design — two YAMLs can share a ``name:`` (a config plus a
        # ``foo (1).yaml`` copy, ``dashboard_import`` siblings, etc.)
        # and a single broadcast must fan out to all of them.
        self._devices_by_name: dict[str, list[Device]] = {}
        # Configuration-keyed shadow index; firmware jobs and metadata
        # mutations resolve a Device by its YAML filename and need an
        # O(1) lookup instead of a linear scan of every configured YAML.
        # Unlike the name index this is 1:1 — a ``configuration`` filename
        # (``path.name``) is unique per tracked path.
        self._devices_by_configuration: dict[str, Device] = {}
        self._cache_keys: dict[Path, _CacheKey] = {}

    @property
    def devices(self) -> list[Device]:
        """Snapshot of the loaded devices in path-iteration order."""
        return list(self._devices.values())

    @property
    def by_path(self) -> Mapping[Path, Device]:
        """Live read-only mapping ``path → Device``.

        Returns a ``MappingProxyType`` view so callers iterate / look
        up but cannot mutate the index out of lockstep with the
        name buckets / cache keys. Mutations must go through
        :meth:`set` / :meth:`pop` / :meth:`rebuild_in_path_order`.
        """
        return MappingProxyType(self._devices)

    def get_by_name(self, name: str) -> list[Device]:
        """Return a fresh-list snapshot of every Device whose ``name`` matches."""
        bucket = self._devices_by_name.get(name)
        return list(bucket) if bucket else []

    def get_by_configuration(self, configuration: str) -> Device | None:
        """Return the Device whose YAML filename equals *configuration*, or ``None``."""
        return self._devices_by_configuration.get(configuration)

    def cache_key(self, path: Path) -> _CacheKey:
        """Return the change-detection cache key for a tracked *path*.

        Raises ``KeyError`` if *path* is not in the index. The
        non-Optional return is the lockstep invariant in API form:
        every path in ``_devices`` has a cache key, and the only
        legitimate caller is one that just verified *path* is
        tracked (e.g. the result of ``find_path_by_filename`` or
        a key in ``by_path``). A miss here is an invariant break,
        not a "look before you leap" miss — surface it loudly
        instead of returning ``None`` (which a caller could pass
        through to ``set`` and silently store as a cache key,
        breaking change detection on the next scan).
        """
        return self._cache_keys[path]

    def find_path_by_filename(self, filename: str) -> Path | None:
        """Locate the tracked path whose ``Path.name`` equals *filename*."""
        return next((p for p in self._devices if p.name == filename), None)

    def set(self, path: Path, device: Device, cache_key: _CacheKey) -> None:
        """Insert / update *device* and refresh its cache key.

        Drops *previous* (if any) from its bucket via
        ``_unindex_name`` first — handles both the same-name update
        path (drop from current bucket so the resort can place the
        fresh Device) and the rename path (drop from the OLD name's
        bucket before bucketing under the new name). Configuration
        filenames are unique per path (the scanner's
        ``util.list_yaml_files`` walk is non-recursive), so once
        *previous* is gone the sorted-position insert never
        collides with an existing entry.
        """
        previous = self._devices.get(path)
        if previous is not None:
            self._unindex_name(previous)
        self._devices[path] = device
        self._cache_keys[path] = cache_key
        # 1:1 by filename, so a same-path in-place edit overwrites its own entry.
        self._devices_by_configuration[device.configuration] = device
        bucket = self._devices_by_name.setdefault(device.name, [])
        insert_at = 0
        while insert_at < len(bucket) and bucket[insert_at].configuration < device.configuration:
            insert_at += 1
        bucket.insert(insert_at, device)

    def pop(self, path: Path) -> Device | None:
        """Drop *path* from every map, returning the removed Device or ``None``."""
        device = self._devices.pop(path, None)
        self._cache_keys.pop(path, None)
        if device is not None:
            self._devices_by_configuration.pop(device.configuration, None)
            self._unindex_name(device)
        return device

    def rebuild_in_path_order(self, path_order: Iterable[Path]) -> None:
        """Re-key ``_devices`` / ``_cache_keys`` in *path_order*.

        ``devices`` is a user-visible read; without this rebuild
        the post-scan iteration order is set-derived and the
        dashboard sees a different device list every restart.
        ``_devices_by_name`` / ``_devices_by_configuration`` don't
        need a parallel re-key — their iteration order isn't
        user-visible and their entries are unchanged here.

        ``path_order`` may carry extra paths that were never
        indexed (a YAML that failed to load is in the scanner's
        ``path_to_cache_key`` but not here) — those are filtered.
        It must NOT omit any currently-indexed path: dropping
        a path from ``_devices`` while leaving it in
        ``_devices_by_name`` would silently break the lockstep
        invariant. Removals must go through :meth:`pop` first.
        """
        ordered = list(path_order)
        ordered_set = set(ordered)
        # Refuse to silently drop any tracked path — that would
        # leave a Device stranded in the name buckets.
        missing = self._devices.keys() - ordered_set
        if missing:
            raise ValueError(
                f"rebuild_in_path_order is missing {len(missing)} indexed path(s); "
                "call pop() before rebuild to remove devices."
            )
        self._devices = {p: self._devices[p] for p in ordered if p in self._devices}
        self._cache_keys = {p: self._cache_keys[p] for p in ordered if p in self._cache_keys}

    def _unindex_name(self, device: Device) -> None:
        # By the lockstep invariant, ``device`` is in its bucket
        # (the only callers — :meth:`set` rename branch and
        # :meth:`pop` — pass devices retrieved from ``_devices``,
        # and every Device in ``_devices`` was placed in its bucket
        # by the matching ``set`` call). No defensive guards needed.
        bucket = self._devices_by_name[device.name]
        bucket.remove(device)
        if not bucket:
            del self._devices_by_name[device.name]


class DeviceScanner(WakeWorker[str]):
    """
    Disk-backed device cache.

    ``scan()`` is safe to call concurrently — overlapping calls coalesce
    via an internal lock. Use ``devices`` to read the current snapshot.
    The :class:`WakeWorker` base drives a background reload worker:
    :meth:`request` queues a filename, :meth:`start` / :meth:`stop`
    manage the task, :meth:`wait_idle` parks until the drain finishes.
    """

    def __init__(
        self,
        config_dir: Path,
        get_metadata: MetadataResolver,
        on_change: ScanCallback,
    ) -> None:
        super().__init__()
        self._config_dir = config_dir
        self._get_metadata = get_metadata
        self._on_change = on_change
        self._index = _DeviceIndex()
        self._lock = asyncio.Lock()

    @property
    def devices(self) -> list[Device]:
        """Snapshot of the currently-loaded devices in lexicographic order.

        ``_do_scan`` re-keys the index in sorted path order so this
        read stays O(n).
        """
        return self._index.devices

    @property
    def by_path(self) -> Mapping[Path, Device]:
        """Live read-only mapping ``path → Device``.

        Forwards to :attr:`_DeviceIndex.by_path`, which returns a
        ``MappingProxyType`` view so external callers can iterate
        / look up but can't mutate the index out of lockstep with
        the name buckets / cache keys.
        """
        return self._index.by_path

    def get_by_name(self, name: str) -> list[Device]:
        """Every configured device whose ``esphome.name`` equals *name*.

        Returns a fresh list (snapshot) — same shape as the
        ``devices`` property. Callers can iterate / mutate the
        return value without corrupting the scanner's internal
        index, and the bucket order is the lexicographic
        configuration-filename order maintained by the index.
        """
        return self._index.get_by_name(name)

    def get_by_configuration(self, configuration: str) -> Device | None:
        """Return the configured device for YAML filename *configuration*, or ``None``."""
        return self._index.get_by_configuration(configuration)

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
        the file isn't tracked. Fires ``ScanChange.RELOADED`` on
        success (the YAML itself is unchanged here).
        """
        async with self._lock:
            path = self._index.find_path_by_filename(filename)
            if path is None:
                return False
            # Snapshot the existing cache key *before* any ``await``
            # so the OSError fallback below is race-free regardless
            # of future lock discipline (today the lock serializes
            # every writer; tomorrow's caller might not respect it).
            # ``cache_key`` returns ``_CacheKey`` (raises ``KeyError``
            # if the path isn't tracked) — the lockstep invariant
            # makes a miss here impossible since
            # ``find_path_by_filename`` just located *path*.
            previous_cache_key = self._index.cache_key(path)
            loaded = await run_in_executor(self._load_devices, {path})
            device = loaded.get(path)
            if device is None:
                return False
            # Refresh the cache key; if the YAML disappears in the
            # race window between load and re-stat, keep the
            # snapshotted previous key so the next ``_do_scan``
            # re-evaluates it.
            try:
                stat = await run_in_executor(path.stat)
                cache_key: _CacheKey = (
                    stat.st_ino,
                    stat.st_dev,
                    stat.st_mtime,
                    stat.st_size,
                )
            except OSError:
                cache_key = previous_cache_key
            self._index.set(path, device, cache_key)
            self._on_change(ScanChange.RELOADED, device)
            return True

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _drain(self) -> None:
        """Reload every filename currently queued."""
        pending, self.pending = self.pending, set()
        for filename in pending:
            try:
                reloaded = await self.reload(filename)
            except Exception:
                _LOGGER.exception("Background reload of %s failed", filename)
                continue
            if not reloaded:
                # Tracked at request time but gone by drain time
                # (concurrent delete / atomic-save mid-flight).
                # Debug-level: the drain doesn't fail, but the
                # vanished save is otherwise invisible.
                _LOGGER.debug(
                    "Background reload skipped: %s is no longer tracked",
                    filename,
                )

    async def _do_scan(self) -> None:
        path_to_cache_key = await run_in_executor(self._build_cache_keys)

        old_paths = set(self._index.by_path.keys())
        new_paths = set(path_to_cache_key.keys())

        removed_paths = old_paths - new_paths
        added_paths = new_paths - old_paths
        possibly_updated = old_paths & new_paths
        updated_paths = {
            p for p in possibly_updated if path_to_cache_key[p] != self._index.cache_key(p)
        }

        paths_to_load = added_paths | updated_paths
        if paths_to_load:
            loaded = await run_in_executor(self._load_devices, paths_to_load)
            for path, device in loaded.items():
                kind = ScanChange.ADDED if path in added_paths else ScanChange.UPDATED
                self._index.set(path, device, path_to_cache_key[path])
                self._on_change(kind, device)

        for path in removed_paths:
            removed_device = self._index.pop(path)
            if removed_device is not None:
                self._on_change(ScanChange.REMOVED, removed_device)

        # Re-key the index in lexicographic-path order so the
        # ``devices`` read returns a stable order across restarts —
        # ``paths_to_load`` was a set so the in-place inserts above
        # ended up in hash-randomised order.
        if added_paths or removed_paths:
            self._index.rebuild_in_path_order(path_to_cache_key.keys())

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
                    metadata.mac_address,
                    metadata.build_size_bytes,
                    metadata.labels,
                    deployed_config_hash=metadata.deployed_config_hash,
                    deployed_version=metadata.deployed_version,
                    api_encryption_active=metadata.api_encryption_active,
                    previous=self._index.by_path.get(path),
                )
            except Exception:  # noqa: BLE001 — best-effort scan; one bad YAML mustn't kill the sweep
                _LOGGER.warning("Failed to load device from %s", path.name)
        return result
