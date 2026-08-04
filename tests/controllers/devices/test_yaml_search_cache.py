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
  internal lock — pinned by counting how many times the read
  helper is invoked under a deliberate race.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from esphome_device_builder.controllers.devices import _yaml_search_cache as cache_module
from esphome_device_builder.controllers.devices._yaml_search_cache import (
    MAX_FILE_BYTES,
    YamlSearchCache,
    _read_for_cache,
)

# ---------------------------------------------------------------------------
# Cold + warm path
# ---------------------------------------------------------------------------


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


async def test_warm_call_returns_cached_without_reading(tmp_path: Path) -> None:
    """Second call against an unchanged file doesn't re-read.

    The whole point of the cache: a debounced keystroke storm
    should hit the disk once per file per mtime, not once per
    keystroke. Pin that contract by replacing the underlying
    read helper after the warm-up call and asserting it never
    fires on the second.
    """
    cache = YamlSearchCache()
    path = tmp_path / "kitchen.yaml"
    path.write_text("wifi:\n", encoding="utf-8")
    first = await cache.get_lines("kitchen.yaml", path)

    # Replace the module's read helper with a sentinel — if the
    # cache calls it, the test fails loudly.
    with patch.object(
        cache_module, "_read_for_cache", side_effect=AssertionError("warm path must not re-read")
    ):
        second = await cache.get_lines("kitchen.yaml", path)

    assert second is first  # same list object — the cache returned the cached one


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


async def test_unreadable_file_returns_none_and_clears_stale(tmp_path: Path) -> None:
    """File stats OK but read fails → ``None`` + cache entry removed.

    Covers the rare race where a YAML is rm'd between the
    cache's ``stat`` and the read (atomic-save churn,
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

    # Now make the read fail on the next call. Stat still works
    # (the path is real); the failure is read-side only.
    with patch.object(cache_module, "_read_for_cache", side_effect=OSError("I/O error mid-read")):
        result = await cache.get_lines("kitchen.yaml", path)

    assert result is None
    # Stale entry was pruned — a follow-up call (with read
    # working again) re-reads from scratch.
    refreshed = await cache.get_lines("kitchen.yaml", path)
    assert refreshed == ["wifi:"]


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

    # a.yaml's entry survived — re-fetch stays on the warm path.
    with patch.object(
        cache_module, "_read_for_cache", side_effect=AssertionError("a.yaml should be cached")
    ):
        await cache.get_lines("a.yaml", a)
    # b.yaml is no longer cached, so a fresh fetch reads — this
    # would raise if it were still cached (above) but works fine
    # here because we removed b.yaml from the cache.
    fresh = await cache.get_lines("b.yaml", b)
    assert fresh == ["wifi:"]


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


async def test_concurrent_misses_against_same_file_read_once(tmp_path: Path) -> None:
    """Two simultaneous misses on the same file collapse to one read.

    Without the cache lock both coroutines would stat + read and
    write the same entry; the duplicate I/O is wasteful but not
    incorrect. Pin the lock-mediated single-read behaviour by
    counting read-helper invocations across a parallel pair.
    """
    cache = YamlSearchCache()
    path = tmp_path / "kitchen.yaml"
    path.write_text("wifi:\n", encoding="utf-8")

    with patch.object(cache_module, "_read_for_cache", wraps=_read_for_cache) as mock_read:
        a, b = await asyncio.gather(
            cache.get_lines("kitchen.yaml", path),
            cache.get_lines("kitchen.yaml", path),
        )

    assert a == b == ["wifi:"]
    # One read, not two — the lock collapsed the second miss into
    # a cache hit once the first finished.
    assert mock_read.call_count == 1


# ---------------------------------------------------------------------------
# Byte-size cap (pathological-file defence)
# ---------------------------------------------------------------------------


async def test_oversize_file_returns_none_without_loading(tmp_path: Path) -> None:
    """Files past ``MAX_FILE_BYTES`` are skipped before the read fires.

    Defends the cache's memory footprint against pathological
    files (machine-generated YAML, accidentally-checked-in build
    output). The on-disk size is checked from ``stat.st_size``;
    if the file is over the cap, the read helper is never called
    and no entry lands in the cache.

    Pin both halves: returns ``None``, AND the read helper was not
    invoked (i.e. we didn't pay the megabyte read cost just to
    immediately drop it).
    """
    cache = YamlSearchCache()
    path = tmp_path / "huge.yaml"
    # One byte past the cap — minimum to exercise the >cap branch.
    path.write_bytes(b"x" * (MAX_FILE_BYTES + 1))

    with patch.object(
        cache_module, "_read_for_cache", side_effect=AssertionError("oversize must not be read")
    ):
        result = await cache.get_lines("huge.yaml", path)

    assert result is None


async def test_oversize_file_drops_stale_cache_entry(tmp_path: Path) -> None:
    """A previously-cached small file is evicted if it grows past the cap.

    The byte cap is checked on every call (not just on the first
    cold one), so a file that lived in the cache while small and
    then grew past the cap drops out cleanly — the next call
    returns ``None`` and the entry is removed. Pin via a third
    call confirming the entry isn't merely shadowed.
    """
    cache = YamlSearchCache()
    path = tmp_path / "kitchen.yaml"
    path.write_text("wifi:\n", encoding="utf-8")
    first = await cache.get_lines("kitchen.yaml", path)
    assert first == ["wifi:"]

    # Grow past the cap and bump mtime so the cache would
    # otherwise miss + re-read.
    path.write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    new_mtime = path.stat().st_mtime_ns + 1_000_000_000
    os.utime(path, ns=(new_mtime, new_mtime))

    second = await cache.get_lines("kitchen.yaml", path)
    assert second is None

    # Shrink back below the cap; the next call should re-populate
    # from scratch (the previous oversize call must have dropped
    # the entry, otherwise we'd see a stale empty / shadow result).
    path.write_text("api:\n", encoding="utf-8")
    new_mtime += 1_000_000_000
    os.utime(path, ns=(new_mtime, new_mtime))
    third = await cache.get_lines("kitchen.yaml", path)
    assert third == ["api:"]


async def test_file_growing_past_cap_between_stat_and_open_is_skipped(
    tmp_path: Path,
) -> None:
    """A grow race slipping past the stat check is caught off the open handle."""
    cache = YamlSearchCache()
    path = tmp_path / "kitchen.yaml"
    path.write_text("wifi:\n", encoding="utf-8")
    first = await cache.get_lines("kitchen.yaml", path)
    assert first == ["wifi:"]
    cached_mtime = path.stat().st_mtime_ns

    path.write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    # A stat snapshot from before the growth: under the cap, mtime
    # differing from the cached key so the read path is reached.
    stale_stat = SimpleNamespace(st_size=7, st_mtime_ns=cached_mtime + 1)

    with patch.object(Path, "stat", return_value=stale_stat):
        second = await cache.get_lines("kitchen.yaml", path)

    assert second is None
    # The entry was dropped, not shadowed: restore the cached mtime
    # onto new content — a surviving stale entry would be returned.
    path.write_text("api:\n", encoding="utf-8")
    os.utime(path, ns=(cached_mtime, cached_mtime))
    third = await cache.get_lines("kitchen.yaml", path)
    assert third == ["api:"]


def test_read_for_cache_bounds_a_file_growing_after_the_fstat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An in-place grow past the cap after the fstat still returns ``None``."""
    path = tmp_path / "kitchen.yaml"
    path.write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    pre_growth = SimpleNamespace(st_size=7, st_mtime_ns=123)
    monkeypatch.setattr(cache_module.os, "fstat", lambda _fd: pre_growth)

    assert _read_for_cache(path) is None


@pytest.mark.skipif(sys.platform == "win32", reason="cannot replace a file with an open handle")
async def test_replace_between_stat_and_read_caches_the_read_version(
    tmp_path: Path,
) -> None:
    """A replace racing the read caches the read bytes under their own mtime."""
    cache = YamlSearchCache()
    path = tmp_path / "kitchen.yaml"
    path.write_text("wifi:\n", encoding="utf-8")

    real_open = Path.open

    def _replace_after_open(self: Path, *args: object, **kwargs: object) -> object:
        fh = real_open(self, *args, **kwargs)
        if self.name == "kitchen.yaml":
            staged = tmp_path / "staged.yaml"
            staged.write_text("api:\n", encoding="utf-8")
            staged.replace(path)
        return fh

    with patch.object(Path, "open", _replace_after_open):
        first = await cache.get_lines("kitchen.yaml", path)

    # The pre-replace handle's bytes, keyed on that version's mtime.
    assert first == ["wifi:"]
    # Guarantee the replacement's mtime differs from the cached key —
    # on a coarse-granularity filesystem the replace can land in the
    # same tick as the original write.
    new_mtime = path.stat().st_mtime_ns + 1_000_000_000
    os.utime(path, ns=(new_mtime, new_mtime))
    # The next call's fresh stat misses the old key and reads the
    # replacement.
    second = await cache.get_lines("kitchen.yaml", path)
    assert second == ["api:"]
