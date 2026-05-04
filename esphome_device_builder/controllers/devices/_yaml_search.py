"""
YAML-content search loop for ``DevicesController.search_yaml``.

Lifted out of the controller so the loop body — walk devices,
read lines from the cache, line-grep, gather hits, respect the
two caps (per-file and total) — has its own home and its own
tests. The controller stays focused on locking and post-search
cache pruning; this module owns the actual search.

No blocking I/O at runtime:

- File reads happen inside ``YamlSearchCache.get_lines`` which
  already wraps ``Path.stat`` / ``Path.read_text`` in
  ``asyncio.to_thread``.
- The per-line substring scan is in-memory against an
  already-split list, so the only "work" between awaits is a
  string comparison. For configs in the small-thousands-of-lines
  range that's microseconds.
- An explicit ``await asyncio.sleep(0)`` between devices yields
  control to the event loop every iteration so a fleet of 100+
  devices doesn't monopolise the run loop while the cache is
  warm (the cache hit path doesn't do I/O so wouldn't otherwise
  yield).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from ._yaml_search_cache import YamlSearchCache


class _DeviceLike(Protocol):
    """The narrow surface ``search_yaml_devices`` reads from each device.

    Pinned as a Protocol rather than depending on the full
    ``Device`` model so the function can be exercised against
    minimal stand-ins in tests, and so a future ``Device`` field
    rename only breaks the controller's call site (which has the
    real type) rather than this module too.
    """

    name: str
    friendly_name: str
    configuration: str


async def search_yaml_devices(
    *,
    devices: Iterable[_DeviceLike],
    cache: YamlSearchCache,
    rel_path: Callable[[str], Path],
    needle: str,
    case_sensitive: bool,
    max_results: int,
    per_file_cap: int,
) -> tuple[list[dict], set[str]]:
    """
    Walk *devices* and return the YAML matches for *needle*.

    Returns a ``(results, live_configurations)`` tuple. The
    caller (``DevicesController.search_yaml``) owns the post-walk
    cache prune against ``live_configurations`` and the
    response-shape contract; this function just produces the
    list.

    Parameters:
    - ``devices`` — iterable of objects exposing ``name``,
      ``friendly_name``, ``configuration``.
    - ``cache`` — the ``YamlSearchCache`` (controller-owned)
      that memoises ``(mtime, lines)`` per file.
    - ``rel_path(configuration)`` — resolves a configuration
      filename to its on-disk ``Path``. Indirected so the
      controller's ``self._db.settings.rel_path`` doesn't leak
      into this module.
    - ``needle`` — the search string. Must be pre-lowered when
      ``case_sensitive`` is False (caller's responsibility — we
      avoid lowering it on every line).
    - ``case_sensitive`` — when False, each line is lowered
      before the substring check.
    - ``max_results`` — hard cap on total matches across the
      fleet. Walk stops once this is reached.
    - ``per_file_cap`` — per-file cap so a chatty match doesn't
      crowd out hits from other devices.

    ``live_configurations`` is the *full* set of input device
    configurations regardless of where the walk short-circuited
    on ``max_results``. The caller uses it to prune cache entries
    for devices that have actually disappeared; if it only
    reflected the walked subset, a capped search would
    spuriously evict warm entries for unwalked-but-still-live
    devices and the next keystroke would re-read them from disk.
    """
    devices_list = list(devices)
    live_configurations: set[str] = {d.configuration for d in devices_list}
    results: list[dict] = []
    total_matches = 0

    for device in devices_list:
        if total_matches >= max_results:
            break

        path = rel_path(device.configuration)
        lines = await cache.get_lines(device.configuration, path)
        if lines is None:
            # Cache hit but no I/O: yield once so a long warm
            # fleet doesn't block the event loop.
            await asyncio.sleep(0)
            continue

        matches: list[dict] = []
        for i, line in enumerate(lines, start=1):
            haystack = line if case_sensitive else line.lower()
            if needle in haystack:
                matches.append({"line_number": i, "line_text": line})
                if len(matches) >= per_file_cap:
                    break
            if total_matches + len(matches) >= max_results:
                break

        if matches:
            results.append(
                {
                    "configuration": device.configuration,
                    "device_name": device.name,
                    "friendly_name": device.friendly_name or device.name,
                    "matches": matches,
                }
            )
            total_matches += len(matches)

        # Yield to the event loop between devices. ``cache.get_lines``
        # already yields on a disk read, but a fully-warm cache hit
        # returns synchronously — without this nudge a 100-device
        # fleet's worth of in-memory line scans would run as a
        # single uninterrupted task slice.
        await asyncio.sleep(0)

    return results, live_configurations
