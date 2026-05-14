"""Tests for the cached build-directory size helper.

The helper walks ``.esphome/build/<device>/`` to compute the total
size of a device's compile artifacts. The walk is heavy + I/O-bound,
so callers gate it behind a freshness pair (``BuildDirSignal``:
``dir_mtime`` + ``build_info_mtime``) and persist the
``(size_bytes, dir_mtime, info_mtime)`` triple in the per-device
metadata sidecar — either side of the pair moving counts as
stale, see ``helpers/build_size.py``'s module docstring for the
empirical matrix that drove the pair-vs-single-stat decision.

These tests cover the helper itself plus the
``BuildSizeRefresher`` worker's behaviour is exercised through
``tests/controllers/firmware/test_refresh.py`` (the
``test_clean_job_skips_full_refresh_but_pokes_build_size`` case
that pins the post-CLEAN refresh hand-off).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from esphome_device_builder.controllers.config import (
    get_device_metadata,
    set_device_metadata,
)
from esphome_device_builder.helpers.build_size import (
    BuildDirSignal,
    coerce_sidecar_int,
    compute_build_dir_size,
    find_stale_build_dirs,
    get_build_dir_mtime,
    refresh_build_size_if_stale,
    resolve_build_dir,
)

# ----------------------------------------------------------------------
# compute_build_dir_size
# ----------------------------------------------------------------------


def test_compute_build_dir_size_sums_files(tmp_path: Path) -> None:
    """Recursive walk sums every regular-file size under the dir."""
    (tmp_path / "a.bin").write_bytes(b"x" * 1024)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.bin").write_bytes(b"y" * 2048)
    (tmp_path / "sub" / "deep").mkdir()
    (tmp_path / "sub" / "deep" / "c.bin").write_bytes(b"z" * 512)

    assert compute_build_dir_size(tmp_path) == 1024 + 2048 + 512


def test_compute_build_dir_size_missing_dir_yields_zero(tmp_path: Path) -> None:
    """A path that doesn't exist returns 0 (not an error).

    The drawer / table read this directly; raising would force
    every caller to wrap the helper in a try/except.
    """
    assert compute_build_dir_size(tmp_path / "does-not-exist") == 0


def test_compute_build_dir_size_empty_dir_yields_zero(tmp_path: Path) -> None:
    """An empty directory contributes nothing to the total."""
    (tmp_path / "empty").mkdir()
    assert compute_build_dir_size(tmp_path / "empty") == 0


def test_compute_build_dir_size_skips_directories(tmp_path: Path) -> None:
    """Only regular files count — directory entries themselves are not summed."""
    (tmp_path / "sub1").mkdir()
    (tmp_path / "sub2").mkdir()
    (tmp_path / "sub3").mkdir()
    # No files anywhere.
    assert compute_build_dir_size(tmp_path) == 0


def test_compute_build_dir_size_swallows_per_entry_errors(tmp_path: Path) -> None:
    """A vanishing file mid-walk doesn't fail the whole operation.

    Concurrent compile cleanup can yank entries between
    ``os.walk`` returning the filename and ``Path.stat()`` reading
    its size. Returning the partial total is better than crashing
    the dashboard.
    """
    (tmp_path / "good.bin").write_bytes(b"x" * 100)
    bad_path = tmp_path / "vanished.bin"
    bad_path.write_bytes(b"y" * 200)

    real_stat = Path.stat

    def fake_stat(self: Path, *args: object, **kwargs: object) -> object:
        if self == bad_path:
            raise OSError("file disappeared")
        return real_stat(self, *args, **kwargs)

    with patch.object(Path, "stat", fake_stat):
        # 100 from good.bin; the bad one is skipped.
        assert compute_build_dir_size(tmp_path) == 100


# ----------------------------------------------------------------------
# get_build_dir_mtime
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1714900000, 1714900000),
        ("1714900000", 1714900000),
        (None, 0),
        (0, 0),
        ("", 0),
        ("not-a-number", 0),
        ("12.7", 0),  # int() rejects fractional strings
        ({}, 0),
        ([], 0),
        (12.9, 12),  # truncates floats — same as ``int()`` on numeric values
    ],
)
def test_coerce_sidecar_int(value: object, expected: int) -> None:
    """``coerce_sidecar_int`` falls back to ``0`` on every shape ``int()`` rejects.

    Same defensive shape both ``find_stale_build_dirs`` /
    ``refresh_build_size_if_stale`` (cached mtimes) and
    ``_resolve_device_metadata`` (cached size) use. Corrupt /
    hand-edited sidecar entries shouldn't crash the per-device
    hot path; the next ``BuildSizeRefresher`` pass repopulates
    fresh values.
    """
    assert coerce_sidecar_int(value) == expected


def test_get_build_dir_mtime_returns_whole_seconds(tmp_path: Path) -> None:
    """The mtime stat is truncated to whole seconds for cross-FS safety.

    Filesystems without sub-second mtime precision (FAT32 / older
    NFS / CIFS) round on write; carrying the float ``st_mtime``
    in the cache would never compare equal after a cross-mount
    move, defeating the cache. Truncating to ``int`` seconds
    here keeps the comparison stable.
    """
    expected = int(tmp_path.stat().st_mtime)
    result = get_build_dir_mtime(tmp_path)
    assert isinstance(result, int)
    assert result == expected


def test_get_build_dir_mtime_missing_dir_yields_zero(tmp_path: Path) -> None:
    """A missing dir returns 0 (sentinel, never matches a real mtime).

    The cache-freshness check in the controller compares this
    against the persisted ``build_size_mtime``; returning 0 for
    a missing dir means the next refresh re-walks (and records 0
    bytes), naturally driving the cached total back to zero
    after the build dir is wiped (e.g. archive flow).
    """
    assert get_build_dir_mtime(tmp_path / "does-not-exist") == 0


# ----------------------------------------------------------------------
# resolve_build_dir
# ----------------------------------------------------------------------


def test_resolve_build_dir_returns_none_when_storage_missing(tmp_path: Path) -> None:
    """No StorageJSON sidecar (device never compiled) → None.

    The helper module guards every other operation behind a None
    check, so callers don't have to special-case the
    pre-first-compile state — they just see ``size = 0``.
    """
    with (
        patch(
            "esphome_device_builder.helpers.build_size.resolve_storage_path",
            return_value=tmp_path / "fake.json",
        ),
        patch(
            "esphome_device_builder.helpers.build_size.StorageJSON.load",
            return_value=None,
        ),
    ):
        assert resolve_build_dir("kitchen.yaml") is None


def test_resolve_build_dir_returns_none_when_build_path_blank(tmp_path: Path) -> None:
    """Older StorageJSON without ``build_path`` populated → None.

    Pre-PIO StorageJSON shapes occasionally land without
    ``build_path``. Treat the same as "no build artifacts."
    """

    class _FakeStorage:
        build_path = ""

    with (
        patch(
            "esphome_device_builder.helpers.build_size.resolve_storage_path",
            return_value=tmp_path / "fake.json",
        ),
        patch(
            "esphome_device_builder.helpers.build_size.StorageJSON.load",
            return_value=_FakeStorage(),
        ),
    ):
        assert resolve_build_dir("kitchen.yaml") is None


def test_resolve_build_dir_returns_path_when_storage_has_build_path(
    tmp_path: Path,
) -> None:
    """A populated ``build_path`` round-trips as a Path."""

    class _FakeStorage:
        build_path = str(tmp_path / "build" / "kitchen")

    with (
        patch(
            "esphome_device_builder.helpers.build_size.resolve_storage_path",
            return_value=tmp_path / "fake.json",
        ),
        patch(
            "esphome_device_builder.helpers.build_size.StorageJSON.load",
            return_value=_FakeStorage(),
        ),
    ):
        result = resolve_build_dir("kitchen.yaml")
        assert result == tmp_path / "build" / "kitchen"


def _fake_storage_patches(tmp_path: Path, build_dir: Path):
    """Patch ``ext_storage_path`` + ``StorageJSON.load`` to point at *build_dir*."""

    class _FakeStorage:
        build_path = str(build_dir)

    return (
        patch(
            "esphome_device_builder.helpers.build_size.resolve_storage_path",
            return_value=tmp_path / "fake.json",
        ),
        patch(
            "esphome_device_builder.helpers.build_size.StorageJSON.load",
            return_value=_FakeStorage(),
        ),
    )


def test_refresh_build_size_if_stale_walks_and_persists_on_first_run(
    tmp_path: Path,
) -> None:
    """No cached pair → walk, persist, and return the new triple.

    Cold-start path: no metadata sidecar entry yet, the build dir
    has files including ``build_info.json``, and the helper
    writes the canonical (size, dir_mtime, info_mtime) triple to
    the sidecar so the next call short-circuits.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    build_dir = tmp_path / "build" / "kitchen"
    build_dir.mkdir(parents=True)
    (build_dir / "firmware.bin").write_bytes(b"x" * 4096)
    (build_dir / "build_info.json").write_text('{"config_hash": "abc"}')

    p1, p2 = _fake_storage_patches(tmp_path, build_dir)
    with p1, p2:
        result = refresh_build_size_if_stale(config_dir, "kitchen.yaml")

    assert result is not None
    body_len = len('{"config_hash": "abc"}')
    assert result.size_bytes == 4096 + body_len
    assert result.signal.dir_mtime == int(build_dir.stat().st_mtime)
    assert result.signal.info_mtime == int((build_dir / "build_info.json").stat().st_mtime)
    # Triple is persisted so the next call short-circuits.
    md = get_device_metadata(config_dir, "kitchen.yaml")
    assert md["build_size_bytes"] == result.size_bytes
    assert md["build_size_dir_mtime"] == result.signal.dir_mtime
    assert md["build_size_info_mtime"] == result.signal.info_mtime


def test_refresh_build_size_if_stale_short_circuits_when_pair_matches(
    tmp_path: Path,
) -> None:
    """Cached pair equals current → return None without walking.

    The whole point of the cache: a steady-state poll should be
    two ``stat()``s per device, never the recursive walk.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    build_dir = tmp_path / "build" / "kitchen"
    build_dir.mkdir(parents=True)
    (build_dir / "firmware.bin").write_bytes(b"x" * 4096)
    (build_dir / "build_info.json").write_text('{"config_hash": "abc"}')
    current_dir = int(build_dir.stat().st_mtime)
    current_info = int((build_dir / "build_info.json").stat().st_mtime)

    # Seed the cache with the current pair + a deliberately-wrong
    # cached size. If the helper walks anyway, the persisted size
    # would be corrected; that's how we detect a regression.
    set_device_metadata(
        config_dir,
        "kitchen.yaml",
        build_size_bytes=999,
        build_size_dir_mtime=current_dir,
        build_size_info_mtime=current_info,
    )

    p1, p2 = _fake_storage_patches(tmp_path, build_dir)
    with p1, p2:
        result = refresh_build_size_if_stale(config_dir, "kitchen.yaml")

    assert result is None
    # The persisted size stayed the deliberately-wrong 999 — proof
    # that the helper short-circuited and didn't walk.
    md = get_device_metadata(config_dir, "kitchen.yaml")
    assert md["build_size_bytes"] == 999


def test_refresh_build_size_if_stale_re_walks_on_dir_mtime_change(
    tmp_path: Path,
) -> None:
    """A bumped dir-mtime alone invalidates the cache (PlatformIO sibling churn)."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    build_dir = tmp_path / "build" / "kitchen"
    build_dir.mkdir(parents=True)
    (build_dir / "firmware.bin").write_bytes(b"x" * 1024)
    (build_dir / "build_info.json").write_text('{"config_hash": "abc"}')

    # Seed with a stale dir mtime; info mtime matches current.
    set_device_metadata(
        config_dir,
        "kitchen.yaml",
        build_size_bytes=999,
        build_size_dir_mtime=int(build_dir.stat().st_mtime) - 1000,
        build_size_info_mtime=int((build_dir / "build_info.json").stat().st_mtime),
    )

    p1, p2 = _fake_storage_patches(tmp_path, build_dir)
    with p1, p2:
        result = refresh_build_size_if_stale(config_dir, "kitchen.yaml")

    assert result is not None
    body_len = len('{"config_hash": "abc"}')
    assert result.size_bytes == 1024 + body_len  # actual, not stale 999


def test_refresh_build_size_if_stale_re_walks_on_info_mtime_change(
    tmp_path: Path,
) -> None:
    """A bumped build_info.json mtime alone invalidates the cache.

    This is the case dir-mtime alone would miss — ESPHome's
    ``write_file_if_changed`` truncates-and-writes the file on a
    real recompile (different config_hash), bumping the file's
    own mtime without touching the parent dir's. Tracking the
    pair catches it; tracking only dir mtime would let the
    drawer / table show a stale size.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    build_dir = tmp_path / "build" / "kitchen"
    build_dir.mkdir(parents=True)
    (build_dir / "firmware.bin").write_bytes(b"x" * 1024)
    (build_dir / "build_info.json").write_text('{"config_hash": "abc"}')

    # Seed with current dir mtime; info mtime stale.
    set_device_metadata(
        config_dir,
        "kitchen.yaml",
        build_size_bytes=999,
        build_size_dir_mtime=int(build_dir.stat().st_mtime),
        build_size_info_mtime=int((build_dir / "build_info.json").stat().st_mtime) - 1000,
    )

    p1, p2 = _fake_storage_patches(tmp_path, build_dir)
    with p1, p2:
        result = refresh_build_size_if_stale(config_dir, "kitchen.yaml")

    assert result is not None
    body_len = len('{"config_hash": "abc"}')
    assert result.size_bytes == 1024 + body_len  # actual, not stale 999


def test_refresh_build_size_if_stale_no_loop_when_build_dir_missing(
    tmp_path: Path,
) -> None:
    """A device whose build dir doesn't exist on disk doesn't re-walk forever.

    Pre-#338 regression risk: ``StorageJSON`` carries a
    ``build_path`` that points at a directory that doesn't
    actually exist (clean checkout, archive flow that didn't
    fully finalise, manual rmtree). Previous logic walked the
    missing path on every poll and ``set_device_metadata`` cleared
    three fields that were already absent — but the non-None
    return triggered a ``scanner.reload`` that would re-fire
    ``DEVICE_UPDATED`` for nothing on every cycle. The pure-pair
    equality check short-circuits ``(0, 0) == (0, 0)`` → fresh →
    no walk, no reload, no churn.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    # Build dir path is recorded in StorageJSON but doesn't exist
    # on disk.
    nonexistent_build_dir = tmp_path / "build" / "kitchen"

    p1, p2 = _fake_storage_patches(tmp_path, nonexistent_build_dir)
    with p1, p2:
        first = refresh_build_size_if_stale(config_dir, "kitchen.yaml")
        # Repeat the call: it must short-circuit, not loop.
        second = refresh_build_size_if_stale(config_dir, "kitchen.yaml")
        third = refresh_build_size_if_stale(config_dir, "kitchen.yaml")

    assert first is None
    assert second is None
    assert third is None
    # And the sidecar stays clean — no 0-valued fields persisted.
    md = get_device_metadata(config_dir, "kitchen.yaml")
    assert "build_size_bytes" not in md
    assert "build_size_dir_mtime" not in md
    assert "build_size_info_mtime" not in md


def test_refresh_build_size_if_stale_clears_cache_when_build_dir_disappears(
    tmp_path: Path,
) -> None:
    """A previously-walked build dir that's been wiped clears the cache once.

    Companion to the no-loop test: when cached values exist but
    the dir is gone (user manually wiped, archive flow ran), the
    helper falls through to the walk *once*, which writes back
    zeros (clearing the cached fields), then subsequent calls
    short-circuit on the (0, 0) == (0, 0) equality.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    # Seed the cache with a populated triple.
    set_device_metadata(
        config_dir,
        "kitchen.yaml",
        build_size_bytes=12345,
        build_size_dir_mtime=1714900000,
        build_size_info_mtime=1714900050,
    )
    nonexistent_build_dir = tmp_path / "build" / "kitchen"

    p1, p2 = _fake_storage_patches(tmp_path, nonexistent_build_dir)
    with p1, p2:
        first = refresh_build_size_if_stale(config_dir, "kitchen.yaml")
        second = refresh_build_size_if_stale(config_dir, "kitchen.yaml")

    # First call: cleared the cache (returned non-None to signal change).
    assert first is not None
    assert first.size_bytes == 0
    assert first.signal == BuildDirSignal(dir_mtime=0, info_mtime=0)
    # Second call: short-circuits on pair equality, no churn.
    assert second is None
    md = get_device_metadata(config_dir, "kitchen.yaml")
    assert "build_size_bytes" not in md
    assert "build_size_dir_mtime" not in md
    assert "build_size_info_mtime" not in md


def test_refresh_build_size_if_stale_works_without_build_info_json(
    tmp_path: Path,
) -> None:
    """Older firmware lacking ``build_info.json`` falls through on dir mtime alone.

    Pre-#16145 builds don't write ``build_info.json``. The
    freshness pair becomes ``(dir_mtime, 0)``; both halves still
    persist verbatim, the cache compares both, and a steady-state
    poll on such a device short-circuits because both halves
    match (``0 == 0``).
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    build_dir = tmp_path / "build" / "kitchen"
    build_dir.mkdir(parents=True)
    (build_dir / "firmware.bin").write_bytes(b"x" * 1024)
    # Deliberately no build_info.json.

    p1, p2 = _fake_storage_patches(tmp_path, build_dir)
    with p1, p2:
        first = refresh_build_size_if_stale(config_dir, "kitchen.yaml")

    assert first is not None
    assert first.size_bytes == 1024
    assert first.signal.dir_mtime > 0
    assert first.signal.info_mtime == 0  # explicit "no build_info.json"
    md = get_device_metadata(config_dir, "kitchen.yaml")
    # ``set_device_metadata`` clears keys passed as 0, so info_mtime
    # is intentionally absent rather than persisted as 0 — the
    # subsequent read defaults to 0 anyway.
    assert md["build_size_dir_mtime"] == first.signal.dir_mtime
    assert "build_size_info_mtime" not in md

    # Second call: nothing changed, so the helper short-circuits.
    p1, p2 = _fake_storage_patches(tmp_path, build_dir)
    with p1, p2:
        second = refresh_build_size_if_stale(config_dir, "kitchen.yaml")
    assert second is None


def test_find_stale_build_dirs_returns_only_divergent_filenames(tmp_path: Path) -> None:
    """Phase-A sweep returns only filenames whose mtime moved past cached.

    Three devices: one with cached mtime that matches the current
    (fresh — must NOT appear), one with a cached mtime older than
    the current (stale — must appear), one with no StorageJSON
    (pre-first-compile — must NOT appear). Order of stale results
    matches the input order.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    fresh_dir = tmp_path / "build" / "fresh"
    fresh_dir.mkdir(parents=True)
    (fresh_dir / "f.bin").write_bytes(b"a" * 100)
    stale_dir = tmp_path / "build" / "stale"
    stale_dir.mkdir(parents=True)
    (stale_dir / "s.bin").write_bytes(b"b" * 200)

    fresh_dir_mtime = int(fresh_dir.stat().st_mtime)
    set_device_metadata(
        config_dir,
        "fresh.yaml",
        build_size_bytes=100,
        build_size_dir_mtime=fresh_dir_mtime,
        # No build_info.json — the absent-info sentinel is 0
        # (set_device_metadata's clear path), which equals the
        # current 0 stat for the missing file. Both halves match.
    )
    set_device_metadata(
        config_dir,
        "stale.yaml",
        build_size_bytes=200,
        build_size_dir_mtime=int(stale_dir.stat().st_mtime) - 1000,
    )

    storage_map = {
        "fresh.yaml": fresh_dir,
        "stale.yaml": stale_dir,
    }

    class _FakeStorage:
        def __init__(self, build_path: str) -> None:
            self.build_path = build_path

    def _fake_load(path):  # type: ignore[no-untyped-def]
        # The mock's ext_storage_path returns one of three sentinel
        # values; map each back to the device's fake build dir.
        # ``never_compiled`` returns None to simulate a missing
        # StorageJSON.
        for filename, build_dir in storage_map.items():
            if path == tmp_path / f"{filename}.json":
                return _FakeStorage(str(build_dir))
        return None

    with (
        patch(
            "esphome_device_builder.helpers.build_size.resolve_storage_path",
            side_effect=lambda f: tmp_path / f"{f}.json",
        ),
        patch(
            "esphome_device_builder.helpers.build_size.StorageJSON.load",
            side_effect=_fake_load,
        ),
    ):
        result = find_stale_build_dirs(
            config_dir, ["fresh.yaml", "stale.yaml", "never_compiled.yaml"]
        )

    assert result == ["stale.yaml"]


def test_find_stale_build_dirs_empty_list_returns_empty(tmp_path: Path) -> None:
    """No devices in → no executor work, no walks, no stale list."""
    assert find_stale_build_dirs(tmp_path, []) == []


def test_find_stale_build_dirs_handles_corrupt_metadata_file(tmp_path: Path) -> None:
    """A metadata read that raises falls back to "no cached data" → all stale.

    A corrupt ``.device-builder.json`` (truncated, permissions
    issue) shouldn't crash the cold-start fleet sweep. The
    helper treats the read failure as "no cached entries", so
    every device whose build dir exists returns as stale on
    that pass.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    build_dir = tmp_path / "build" / "kitchen"
    build_dir.mkdir(parents=True)

    class _FakeStorage:
        build_path = str(build_dir)

    with (
        patch(
            "esphome_device_builder.controllers.config._load_metadata",
            side_effect=OSError("permission denied"),
        ),
        patch(
            "esphome_device_builder.helpers.build_size.resolve_storage_path",
            return_value=tmp_path / "fake.json",
        ),
        patch(
            "esphome_device_builder.helpers.build_size.StorageJSON.load",
            return_value=_FakeStorage(),
        ),
    ):
        result = find_stale_build_dirs(config_dir, ["kitchen.yaml"])

    # No cached pair → drift detected → kitchen flagged stale.
    assert result == ["kitchen.yaml"]


def test_refresh_build_size_if_stale_returns_none_when_no_storage(tmp_path: Path) -> None:
    """Pre-first-compile devices (no StorageJSON) skip the whole pipeline."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    with (
        patch(
            "esphome_device_builder.helpers.build_size.resolve_storage_path",
            return_value=tmp_path / "fake.json",
        ),
        patch(
            "esphome_device_builder.helpers.build_size.StorageJSON.load",
            return_value=None,
        ),
    ):
        assert refresh_build_size_if_stale(config_dir, "kitchen.yaml") is None
    # And no sidecar entry was created either.
    assert get_device_metadata(config_dir, "kitchen.yaml") == {}


def test_resolve_build_dir_returns_none_when_storage_load_raises(tmp_path: Path) -> None:
    """A corrupt StorageJSON returns None instead of propagating.

    ``StorageJSON.load`` returns ``None`` on a missing file but
    raises on a malformed one. The drawer renders for every
    device, so a single corrupt sidecar shouldn't fail the whole
    list — treat the same as "no build artifacts."
    """
    with (
        patch(
            "esphome_device_builder.helpers.build_size.resolve_storage_path",
            return_value=tmp_path / "fake.json",
        ),
        patch(
            "esphome_device_builder.helpers.build_size.StorageJSON.load",
            side_effect=ValueError("malformed"),
        ),
    ):
        assert resolve_build_dir("kitchen.yaml") is None
