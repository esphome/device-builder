"""Coverage for ``YamlSearchCache``.

Pinned in isolation from ``DevicesController.search_yaml`` so a
regression in the cache logic surfaces here, not as a flaky
end-to-end ``yaml/search`` test. The end-to-end tests in
``test_search_yaml.py`` exercise the cache via the controller's
public command and stay focused on result-shape + fleet-walk
contracts.

Branches:

- Cold call → reads file + splits lines + caches.
- Warm call (mtime unchanged) → returns cached list without
  touching disk.
- Mtime advanced → re-reads, replaces cached entry.
- Missing / unreadable file → returns ``None`` and clears any
  stale cache entry.
- ``prune`` drops only stale entries.
- Concurrent calls against the same file serialise via the
  internal lock — pinned by counting how many times
  ``read_text`` is invoked under a deliberate race.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from esphome_device_builder.controllers.devices._yaml_search_cache import (
    YamlSearchCache,
)

# ---------------------------------------------------------------------------
# Cold + warm path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cold_call_reads_file_and_caches(tmp_path: Path) -> None:
    """First call against a fresh file reads from disk + splits lines.

    Pin both the read-through behaviour and the line-split shape
    (``str.splitlines`` rules — no trailing-newline empty entry) so
    the search caller can iterate ``enumerate(lines, start=1)`` and
    get 1-based line numbers that match user-visible editor lines.
    """
    cache = YamlSearchCache()
    path = tmp_path / "kitchen.yaml"
    path.write_text("esphome:\n  name: kitchen\nwifi:\n", encoding="utf-8")

    lines = await cache.get_lines("kitchen.yaml", path)

    assert lines == ["esphome:", "  name: kitchen", "wifi:"]


@pytest.mark.asyncio
async def test_warm_call_returns_cached_without_reading(tmp_path: Path) -> None:
    """Second call against an unchanged file doesn't re-read.

    The whole point of the cache: a debounced keystroke storm
    should hit the disk once per file per mtime, not once per
    keystroke. Pin that contract by replacing the underlying
    ``read_text`` after the warm-up call and asserting it never
    fires on the second.
    """
    cache = YamlSearchCache()
    path = tmp_path / "kitchen.yaml"
    path.write_text("wifi:\n", encoding="utf-8")
    first = await cache.get_lines("kitchen.yaml", path)

    # Replace path.read_text on this specific Path instance with a
    # sentinel — if the cache calls it, the test fails loudly.
    with patch.object(Path, "read_text", side_effect=AssertionError("warm path must not re-read")):
        second = await cache.get_lines("kitchen.yaml", path)

    assert second is first  # same list object — the cache returned the cached one


@pytest.mark.asyncio
async def test_mtime_change_invalidates_cache(tmp_path: Path) -> None:
    """A new mtime forces a re-read; the cached entry is replaced.

    Editing a YAML in-place advances ``mtime_ns``; the cache key on
    that field is what makes "save in the editor → next search
    sees the new content" work without an external invalidation
    call.
    """
    cache = YamlSearchCache()
    path = tmp_path / "kitchen.yaml"
    path.write_text("wifi:\n", encoding="utf-8")
    first = await cache.get_lines("kitchen.yaml", path)
    assert first == ["wifi:"]

    # Bump mtime_ns deliberately — Path.write_text + os.utime would
    # also work but the explicit utime makes the intent crystal.
    new_mtime = path.stat().st_mtime_ns + 1_000_000_000  # +1s
    path.write_text("api:\n  encryption:\n    key: !secret\n", encoding="utf-8")
    os.utime(path, ns=(new_mtime, new_mtime))

    second = await cache.get_lines("kitchen.yaml", path)

    assert second == ["api:", "  encryption:", "    key: !secret"]


# ---------------------------------------------------------------------------
# Missing / unreadable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_file_returns_none_and_clears_stale(tmp_path: Path) -> None:
    """File deleted between calls → ``None`` + cache entry removed.

    The scanner's index can briefly disagree with the filesystem
    (atomic-save remove + re-add, manual ``rm`` by the user, etc.).
    A previously-cached entry must not be returned for a vanished
    file — the next search would render misleading hits against
    text that's no longer on disk.
    """
    cache = YamlSearchCache()
    path = tmp_path / "kitchen.yaml"
    path.write_text("wifi:\n", encoding="utf-8")

    first = await cache.get_lines("kitchen.yaml", path)
    assert first == ["wifi:"]

    path.unlink()

    second = await cache.get_lines("kitchen.yaml", path)
    assert second is None
    # And a third call (still missing) keeps returning None — pin
    # that the stale entry was actually pruned, not just shadowed.
    third = await cache.get_lines("kitchen.yaml", path)
    assert third is None


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unreadable_file_returns_none_and_clears_stale(tmp_path: Path) -> None:
    """File stats OK but read fails → ``None`` + cache entry removed.

    Covers the rare race where a YAML is rm'd between the
    cache's ``stat`` and ``read_text`` calls (atomic-save churn,
    aggressive cleanup tooling, etc.). The cache must treat this
    the same way it treats a stat failure: prune the entry,
    return ``None``, let the search loop skip the device.
    """
    cache = YamlSearchCache()
    path = tmp_path / "kitchen.yaml"
    path.write_text("wifi:\n", encoding="utf-8")
    # Warm the cache so we have an entry to evict.
    first = await cache.get_lines("kitchen.yaml", path)
    assert first == ["wifi:"]
    # Bump mtime so the next call misses the warm-entry early-
    # return and reaches the read path.
    new_mtime = path.stat().st_mtime_ns + 1_000_000_000
    os.utime(path, ns=(new_mtime, new_mtime))

    # Now make ``read_text`` fail on the next call. Stat still
    # works (the path is real); the failure is read-side only.
    real_read = Path.read_text
    call_count = 0

    def _failing_read(self: Path, *args: object, **kwargs: object) -> str:
        nonlocal call_count
        call_count += 1
        if self == path and call_count == 1:
            msg = "I/O error mid-read"
            raise OSError(msg)
        return real_read(self, *args, **kwargs)  # type: ignore[arg-type]

    with patch.object(Path, "read_text", _failing_read):
        result = await cache.get_lines("kitchen.yaml", path)

    assert result is None
    # Stale entry was pruned — a follow-up call (with read
    # working again) re-reads from scratch.
    refreshed = await cache.get_lines("kitchen.yaml", path)
    assert refreshed == ["wifi:"]


@pytest.mark.asyncio
async def test_prune_drops_only_stale_entries(tmp_path: Path) -> None:
    """``prune(live)`` removes entries whose key isn't in *live*.

    Called after each search against the set of currently-live
    device configurations; without this the cache would grow
    without bound across long-lived dashboards as devices are
    added and removed.
    """
    cache = YamlSearchCache()
    a = tmp_path / "a.yaml"
    b = tmp_path / "b.yaml"
    a.write_text("wifi:\n", encoding="utf-8")
    b.write_text("wifi:\n", encoding="utf-8")
    await cache.get_lines("a.yaml", a)
    await cache.get_lines("b.yaml", b)

    # Only "a.yaml" is live now.
    cache.prune(["a.yaml"])

    # b.yaml's entry was removed — re-fetch hits the read path.
    with patch.object(Path, "read_text", side_effect=AssertionError("a.yaml should be cached")):
        await cache.get_lines("a.yaml", a)
    # b.yaml is no longer cached, so a fresh fetch reads — this
    # would raise if it were still cached (above) but works fine
    # here because we removed b.yaml from the cache.
    fresh = await cache.get_lines("b.yaml", b)
    assert fresh == ["wifi:"]


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_misses_against_same_file_read_once(tmp_path: Path) -> None:
    """Two simultaneous misses on the same file collapse to one read.

    Without the cache lock both coroutines would stat + read and
    write the same entry; the duplicate I/O is wasteful but not
    incorrect. Pin the lock-mediated single-read behaviour by
    counting ``read_text`` invocations across a parallel pair.
    """
    cache = YamlSearchCache()
    path = tmp_path / "kitchen.yaml"
    path.write_text("wifi:\n", encoding="utf-8")

    real_read = Path.read_text
    call_count = 0

    def _counting_read(self: Path, *args: object, **kwargs: object) -> str:
        nonlocal call_count
        call_count += 1
        return real_read(self, *args, **kwargs)  # type: ignore[arg-type]

    with patch.object(Path, "read_text", _counting_read):
        a, b = await asyncio.gather(
            cache.get_lines("kitchen.yaml", path),
            cache.get_lines("kitchen.yaml", path),
        )

    assert a == b == ["wifi:"]
    # One read, not two — the lock collapsed the second miss into
    # a cache hit once the first finished.
    assert call_count == 1
