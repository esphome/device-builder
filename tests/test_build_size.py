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


def test_refresh_build_size_if_stale_walks_and_returns_triple_on_first_run(
    tmp_path: Path,
) -> None:
    """No cached pair → walk, return the new triple.

    Cold-start path: the caller's cached signal is the
    ``(0, 0)`` sentinel; the build dir has files including
    ``build_info.json``, and the helper returns the canonical
    (size, dir_mtime, info_mtime) triple. Persistence is the
    caller's job — covered separately in the refresher tests.
    """
    build_dir = tmp_path / "build" / "kitchen"
    build_dir.mkdir(parents=True)
    (build_dir / "firmware.bin").write_bytes(b"x" * 4096)
    (build_dir / "build_info.json").write_text('{"config_hash": "abc"}')

    p1, p2 = _fake_storage_patches(tmp_path, build_dir)
    with p1, p2:
        result = refresh_build_size_if_stale(
            "kitchen.yaml", BuildDirSignal(dir_mtime=0, info_mtime=0)
        )

    assert result is not None
    body_len = len('{"config_hash": "abc"}')
    assert result.size_bytes == 4096 + body_len
    assert result.signal.dir_mtime == int(build_dir.stat().st_mtime)
    assert result.signal.info_mtime == int((build_dir / "build_info.json").stat().st_mtime)


def test_refresh_build_size_if_stale_short_circuits_when_pair_matches(
    tmp_path: Path,
) -> None:
    """Cached pair equals current → return None without walking.

    The whole point of the cache: a steady-state poll should be
    two ``stat()``s per device, never the recursive walk.
    """
    build_dir = tmp_path / "build" / "kitchen"
    build_dir.mkdir(parents=True)
    (build_dir / "firmware.bin").write_bytes(b"x" * 4096)
    (build_dir / "build_info.json").write_text('{"config_hash": "abc"}')
    cached = BuildDirSignal(
        dir_mtime=int(build_dir.stat().st_mtime),
        info_mtime=int((build_dir / "build_info.json").stat().st_mtime),
    )

    p1, p2 = _fake_storage_patches(tmp_path, build_dir)
    with p1, p2:
        result = refresh_build_size_if_stale("kitchen.yaml", cached)

    assert result is None


def test_refresh_build_size_if_stale_re_walks_on_dir_mtime_change(
    tmp_path: Path,
) -> None:
    """A bumped dir-mtime alone invalidates the cache (PlatformIO sibling churn)."""
    build_dir = tmp_path / "build" / "kitchen"
    build_dir.mkdir(parents=True)
    (build_dir / "firmware.bin").write_bytes(b"x" * 1024)
    (build_dir / "build_info.json").write_text('{"config_hash": "abc"}')

    # Cached: stale dir mtime; info mtime matches current.
    cached = BuildDirSignal(
        dir_mtime=int(build_dir.stat().st_mtime) - 1000,
        info_mtime=int((build_dir / "build_info.json").stat().st_mtime),
    )

    p1, p2 = _fake_storage_patches(tmp_path, build_dir)
    with p1, p2:
        result = refresh_build_size_if_stale("kitchen.yaml", cached)

    assert result is not None
    body_len = len('{"config_hash": "abc"}')
    assert result.size_bytes == 1024 + body_len


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
    build_dir = tmp_path / "build" / "kitchen"
    build_dir.mkdir(parents=True)
    (build_dir / "firmware.bin").write_bytes(b"x" * 1024)
    (build_dir / "build_info.json").write_text('{"config_hash": "abc"}')

    cached = BuildDirSignal(
        dir_mtime=int(build_dir.stat().st_mtime),
        info_mtime=int((build_dir / "build_info.json").stat().st_mtime) - 1000,
    )

    p1, p2 = _fake_storage_patches(tmp_path, build_dir)
    with p1, p2:
        result = refresh_build_size_if_stale("kitchen.yaml", cached)

    assert result is not None
    body_len = len('{"config_hash": "abc"}')
    assert result.size_bytes == 1024 + body_len


def test_refresh_build_size_if_stale_no_loop_when_build_dir_missing(
    tmp_path: Path,
) -> None:
    """Build dir doesn't exist + empty cache → short-circuit ``(0,0)==(0,0)``.

    ``StorageJSON`` may carry a ``build_path`` pointing at a
    directory that doesn't actually exist (clean checkout,
    archive flow that didn't fully finalise, manual rmtree).
    The pure-pair equality check short-circuits on the
    all-zero sentinel — no walk, no caller-visible churn.
    """
    nonexistent_build_dir = tmp_path / "build" / "kitchen"
    empty = BuildDirSignal(dir_mtime=0, info_mtime=0)

    p1, p2 = _fake_storage_patches(tmp_path, nonexistent_build_dir)
    with p1, p2:
        first = refresh_build_size_if_stale("kitchen.yaml", empty)
        second = refresh_build_size_if_stale("kitchen.yaml", empty)
        third = refresh_build_size_if_stale("kitchen.yaml", empty)

    assert first is None
    assert second is None
    assert third is None


def test_refresh_build_size_if_stale_clears_cache_when_build_dir_disappears(
    tmp_path: Path,
) -> None:
    """Populated cache + vanished dir → returns the zero triple once.

    Companion to the no-loop test: when the cached signal is
    non-empty but the dir is gone, the helper returns
    ``BuildSizeRefreshResult(size_bytes=0, signal=(0, 0))``
    so the caller can clear its cached triple. The next call
    (cache now zero, dir still missing) short-circuits.
    """
    nonexistent_build_dir = tmp_path / "build" / "kitchen"
    populated = BuildDirSignal(dir_mtime=1714900000, info_mtime=1714900050)
    empty = BuildDirSignal(dir_mtime=0, info_mtime=0)

    p1, p2 = _fake_storage_patches(tmp_path, nonexistent_build_dir)
    with p1, p2:
        first = refresh_build_size_if_stale("kitchen.yaml", populated)
        second = refresh_build_size_if_stale("kitchen.yaml", empty)

    assert first is not None
    assert first.size_bytes == 0
    assert first.signal == BuildDirSignal(dir_mtime=0, info_mtime=0)
    assert second is None


def test_refresh_build_size_if_stale_works_without_build_info_json(
    tmp_path: Path,
) -> None:
    """Older firmware lacking ``build_info.json`` falls through on dir mtime alone.

    Pre-#16145 builds don't write ``build_info.json``. The
    freshness pair becomes ``(dir_mtime, 0)``; the cache compares
    both halves, and a steady-state poll on such a device
    short-circuits because both halves match (``0 == 0``).
    """
    build_dir = tmp_path / "build" / "kitchen"
    build_dir.mkdir(parents=True)
    (build_dir / "firmware.bin").write_bytes(b"x" * 1024)
    # Deliberately no build_info.json.

    p1, p2 = _fake_storage_patches(tmp_path, build_dir)
    with p1, p2:
        first = refresh_build_size_if_stale(
            "kitchen.yaml", BuildDirSignal(dir_mtime=0, info_mtime=0)
        )

    assert first is not None
    assert first.size_bytes == 1024
    assert first.signal.dir_mtime > 0
    assert first.signal.info_mtime == 0

    # Second call with the post-walk pair as cache → no walk.
    p1, p2 = _fake_storage_patches(tmp_path, build_dir)
    with p1, p2:
        second = refresh_build_size_if_stale("kitchen.yaml", first.signal)
    assert second is None


def test_find_stale_build_dirs_returns_only_divergent_filenames(tmp_path: Path) -> None:
    """Phase-A sweep returns only filenames whose mtime moved past cached.

    Three devices: one with cached mtime that matches the current
    (fresh — must NOT appear), one with a cached mtime older than
    the current (stale — must appear), one with no StorageJSON
    (pre-first-compile — must NOT appear). Order of stale results
    matches the input order.
    """
    fresh_dir = tmp_path / "build" / "fresh"
    fresh_dir.mkdir(parents=True)
    (fresh_dir / "f.bin").write_bytes(b"a" * 100)
    stale_dir = tmp_path / "build" / "stale"
    stale_dir.mkdir(parents=True)
    (stale_dir / "s.bin").write_bytes(b"b" * 200)

    metadata = {
        "fresh.yaml": {
            "build_size_bytes": 100,
            "build_size_dir_mtime": int(fresh_dir.stat().st_mtime),
            # No build_info.json — both halves end up 0.
        },
        "stale.yaml": {
            "build_size_bytes": 200,
            "build_size_dir_mtime": int(stale_dir.stat().st_mtime) - 1000,
        },
    }

    storage_map = {
        "fresh.yaml": fresh_dir,
        "stale.yaml": stale_dir,
    }

    class _FakeStorage:
        def __init__(self, build_path: str) -> None:
            self.build_path = build_path

    def _fake_load(path):  # type: ignore[no-untyped-def]
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
            ["fresh.yaml", "stale.yaml", "never_compiled.yaml"],
            metadata,
        )

    assert result == ["stale.yaml"]


def test_find_stale_build_dirs_empty_list_returns_empty() -> None:
    """No devices in → no executor work, no walks, no stale list."""
    assert find_stale_build_dirs([], {}) == []


def test_find_stale_build_dirs_empty_metadata_marks_devices_stale(tmp_path: Path) -> None:
    """Empty metadata dict → every device whose build dir exists is stale.

    Mirrors the cold-start path: the store has no entries yet
    (fresh install or post-migration), so every device's cached
    signal is the ``(0, 0)`` sentinel and the actual dir
    differs.
    """
    build_dir = tmp_path / "build" / "kitchen"
    build_dir.mkdir(parents=True)

    class _FakeStorage:
        build_path = str(build_dir)

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
        result = find_stale_build_dirs(["kitchen.yaml"], {})

    assert result == ["kitchen.yaml"]


def test_refresh_build_size_if_stale_returns_none_when_no_storage(tmp_path: Path) -> None:
    """Pre-first-compile devices (no StorageJSON) skip the whole pipeline."""
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
        assert (
            refresh_build_size_if_stale("kitchen.yaml", BuildDirSignal(dir_mtime=0, info_mtime=0))
            is None
        )


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
