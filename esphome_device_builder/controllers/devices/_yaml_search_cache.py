"""
Per-file cache for the ``yaml/search`` command's line buffer.

The frontend dropdown debounces keystrokes but still fires one
search per keystroke pause — on a fleet of 100 devices that's 100
reads + 100 ``str.splitlines`` calls per keystroke without a
cache. This class memoises the per-file ``(mtime_ns, lines)``
tuple so subsequent searches against an unchanged file become
pure ``stat`` + already-split list scan.

Bounded by the device count: keys are configuration filenames, and
``prune`` is called after each search to drop entries for devices
that have been deleted / archived between calls. Populated lazily
on the first cache miss.

A single ``asyncio.Lock`` serialises the stat-check-read-update
critical section so two concurrent searches against the same file
can't both miss and both spawn a duplicate read. Per-file locks
were considered but the critical section is short (a stat plus
at most one small file read) and the contention happens in
sequence across the fleet anyway, so one lock keeps the
bookkeeping minimal.

Lifted out of ``DevicesController`` so the search bookkeeping
isn't tangled with device CRUD; tests can target the cache in
isolation.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from pathlib import Path

_LOGGER = logging.getLogger(__name__)


# Hard byte ceiling on any single file the search cache will
# load into memory. Files past this size are *not* loaded — the
# cache returns ``None`` and the search loop skips them.
#
# 8 MiB comfortably holds every realistic ESPHome config (the
# largest packaged ratgdo / Apollo configs land around 100 KiB
# even with hundreds of binary_sensor blocks). The cap exists
# to defend against pathological cases — machine-generated
# YAML, accidentally-checked-in build output, runaway lambda
# blocks producing megabytes of source — that would otherwise
# pin tens or hundreds of megabytes per device into the cache
# for a feature that's only meant to surface line-grep matches.
#
# Pairs with ``_yaml_search.MAX_LINES_PER_FILE``: the line cap
# bounds *scan* time, this byte cap bounds *memory*. Both are
# sized so realistic configs are unaffected.
MAX_FILE_BYTES = 8 * 1024 * 1024


class YamlSearchCache:
    """Async-safe (mtime, lines) cache for raw device YAML files."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[int, list[str]]] = {}
        self._lock = asyncio.Lock()

    async def get_lines(self, configuration: str, path: Path) -> list[str] | None:
        """
        Return the file's split lines, served from cache when fresh.

        Stats *path*; if a cached entry's mtime matches, returns
        the previously-split lines without touching disk again.
        Otherwise reads + splits and stores the result.

        Returns ``None`` (and removes any stale cache entry) when:

        - the file is gone or unreadable (errors logged at DEBUG —
          they're routine in a fleet that's actively being edited
          — and never propagate; ``yaml/search`` is best-effort
          across the fleet, not a per-device contract);
        - the file's on-disk size exceeds ``MAX_FILE_BYTES``. The
          byte ceiling is checked from ``stat.st_size`` *before*
          ``read_text``, so a pathological multi-megabyte YAML
          never gets loaded into Python memory in the first place
          — the cache stays bounded regardless of what the
          filesystem holds.
        """
        async with self._lock:
            try:
                stat = await asyncio.to_thread(path.stat)
            except OSError as exc:
                _LOGGER.debug("yaml-search-cache: stat %s failed: %s", configuration, exc)
                self._entries.pop(configuration, None)
                return None

            if stat.st_size > MAX_FILE_BYTES:
                _LOGGER.debug(
                    "yaml-search-cache: %s is %d bytes, over %d-byte cap — skipped",
                    configuration,
                    stat.st_size,
                    MAX_FILE_BYTES,
                )
                # Drop any stale entry from a previous call when
                # the file was smaller — the search loop should
                # treat the device as unreadable now.
                self._entries.pop(configuration, None)
                return None

            cached = self._entries.get(configuration)
            if cached is not None and cached[0] == stat.st_mtime_ns:
                return cached[1]

            try:
                text = await asyncio.to_thread(path.read_text, encoding="utf-8", errors="replace")
            except OSError as exc:
                _LOGGER.debug("yaml-search-cache: read %s failed: %s", configuration, exc)
                self._entries.pop(configuration, None)
                return None

            lines = text.splitlines()
            self._entries[configuration] = (stat.st_mtime_ns, lines)
            return lines

    def prune(self, live_configurations: Iterable[str]) -> None:
        """Drop entries for configurations not in *live_configurations*.

        Called after each search so the cache shrinks when devices
        are deleted / archived; without this the dict would grow
        without bound across long-lived dashboards.
        """
        live = set(live_configurations)
        for stale in [k for k in self._entries if k not in live]:
            del self._entries[stale]
