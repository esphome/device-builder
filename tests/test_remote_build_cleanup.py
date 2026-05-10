"""
Tests for the receiver-side TTL cleanup sweep (issue #106 phase 6c).

Drives the helper directly against real on-disk subtrees + bundle
tarballs constructed under :class:`tmp_path`; the controller's
periodic loop is a thin executor-hop around this function so the
disk-side branches all surface here.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from esphome_device_builder.helpers.remote_build_cleanup import sweep_remote_builds
from esphome_device_builder.helpers.remote_build_layout import (
    REMOTE_BUILDS_SUBDIR,
    RemoteBuildPath,
)


def _populate(config_dir: Path, key: RemoteBuildPath, *, age_seconds: float, now: float) -> None:
    """Create a subtree + sibling bundle under *config_dir* aged by *age_seconds*.

    Stamps mtime to ``now - age_seconds`` on the subtree so the
    sweep's ``st_mtime`` check has a deterministic value to
    compare against ``now - ttl``. Bundle stays at its natural
    write time; the sweep keys on the subtree, not the bundle.
    """
    subtree = key.subtree(config_dir)
    subtree.mkdir(parents=True, exist_ok=True)
    (subtree / "kitchen.yaml").write_bytes(b"esphome:\n  name: kitchen\n")
    key.bundle(config_dir).write_bytes(b"fake bundle bytes")
    target_mtime = now - age_seconds
    os.utime(subtree, (target_mtime, target_mtime))


def test_sweep_returns_zero_on_missing_remote_builds_root(tmp_path: Path) -> None:
    """A fresh receiver with no submissions yet → no-op + zero deletes."""
    assert sweep_remote_builds(tmp_path, ttl_seconds=10, in_flight_keys=frozenset()) == 0


def test_sweep_deletes_subtree_and_bundle_when_cold(tmp_path: Path) -> None:
    """Cold subtree → deleted + sibling bundle gone."""
    now = 1_000_000.0
    key = RemoteBuildPath(dashboard_id="alpha", device_name="kitchen")
    _populate(tmp_path, key, age_seconds=3600, now=now)

    deleted = sweep_remote_builds(tmp_path, ttl_seconds=600, in_flight_keys=frozenset(), now=now)
    assert deleted == 1
    assert not key.subtree(tmp_path).exists()
    assert not key.bundle(tmp_path).exists()


def test_sweep_keeps_fresh_subtree(tmp_path: Path) -> None:
    """A subtree within the TTL window stays untouched."""
    now = 1_000_000.0
    key = RemoteBuildPath(dashboard_id="alpha", device_name="kitchen")
    _populate(tmp_path, key, age_seconds=60, now=now)

    deleted = sweep_remote_builds(tmp_path, ttl_seconds=600, in_flight_keys=frozenset(), now=now)
    assert deleted == 0
    assert key.subtree(tmp_path).is_dir()
    assert key.bundle(tmp_path).is_file()


def test_sweep_skips_in_flight_even_when_cold(tmp_path: Path) -> None:
    """A cold subtree still in-flight stays — defense-in-depth gate."""
    now = 1_000_000.0
    key = RemoteBuildPath(dashboard_id="alpha", device_name="kitchen")
    _populate(tmp_path, key, age_seconds=3600, now=now)

    deleted = sweep_remote_builds(
        tmp_path,
        ttl_seconds=600,
        in_flight_keys=frozenset({key}),
        now=now,
    )
    assert deleted == 0
    assert key.subtree(tmp_path).is_dir()


def test_sweep_prunes_empty_dashboard_parent(tmp_path: Path) -> None:
    """After the last device under a dashboard_id is swept, prune the parent."""
    now = 1_000_000.0
    key = RemoteBuildPath(dashboard_id="alpha", device_name="kitchen")
    _populate(tmp_path, key, age_seconds=3600, now=now)

    sweep_remote_builds(tmp_path, ttl_seconds=600, in_flight_keys=frozenset(), now=now)
    parent = tmp_path / REMOTE_BUILDS_SUBDIR / "alpha"
    assert not parent.exists()


def test_sweep_keeps_dashboard_parent_when_sibling_still_warm(tmp_path: Path) -> None:
    """A dashboard with one cold + one warm device keeps the parent."""
    now = 1_000_000.0
    cold = RemoteBuildPath(dashboard_id="alpha", device_name="kitchen")
    warm = RemoteBuildPath(dashboard_id="alpha", device_name="bedroom")
    _populate(tmp_path, cold, age_seconds=3600, now=now)
    _populate(tmp_path, warm, age_seconds=60, now=now)

    deleted = sweep_remote_builds(tmp_path, ttl_seconds=600, in_flight_keys=frozenset(), now=now)
    assert deleted == 1
    assert not cold.subtree(tmp_path).exists()
    assert warm.subtree(tmp_path).is_dir()
    assert (tmp_path / REMOTE_BUILDS_SUBDIR / "alpha").is_dir()


def test_sweep_handles_multiple_dashboards(tmp_path: Path) -> None:
    """Sweep walks every dashboard_id parent independently."""
    now = 1_000_000.0
    alpha_kitchen = RemoteBuildPath(dashboard_id="alpha", device_name="kitchen")
    beta_kitchen = RemoteBuildPath(dashboard_id="beta", device_name="kitchen")
    _populate(tmp_path, alpha_kitchen, age_seconds=3600, now=now)
    _populate(tmp_path, beta_kitchen, age_seconds=60, now=now)

    deleted = sweep_remote_builds(tmp_path, ttl_seconds=600, in_flight_keys=frozenset(), now=now)
    assert deleted == 1
    assert not alpha_kitchen.subtree(tmp_path).exists()
    assert beta_kitchen.subtree(tmp_path).is_dir()


def test_sweep_ignores_stray_files_under_root(tmp_path: Path) -> None:
    """A stray non-directory under the remote-builds root is left alone.

    Operator hand-edit, foreign file. The sweep walks
    directories; bare files at the dashboard-level or under
    a dashboard are skipped (the iterdir loop's ``is_dir``
    guard handles them).
    """
    now = 1_000_000.0
    root = tmp_path / REMOTE_BUILDS_SUBDIR
    root.mkdir(parents=True)
    stray = root / "readme.txt"
    stray.write_text("hands off")

    deleted = sweep_remote_builds(tmp_path, ttl_seconds=600, in_flight_keys=frozenset(), now=now)
    assert deleted == 0
    assert stray.is_file()


def test_sweep_continues_after_subtree_rmtree_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing rmtree on one subtree doesn't abort the rest of the sweep.

    Permission errors / races against a concurrent submit /
    broken symlinks in the tree all happen in production; the
    sweep is best-effort hygiene, a single bad subtree
    shouldn't poison the rest. Monkeypatches ``shutil.rmtree``
    to fail on the first call and succeed on the second; the
    second cold subtree should still get reclaimed.
    """
    now = 1_000_000.0
    first = RemoteBuildPath(dashboard_id="alpha", device_name="kitchen")
    second = RemoteBuildPath(dashboard_id="alpha", device_name="bedroom")
    _populate(tmp_path, first, age_seconds=3600, now=now)
    _populate(tmp_path, second, age_seconds=3600, now=now)

    real_rmtree = __import__("shutil").rmtree
    calls: list[Path] = []

    def _flaky(path: str | Path, *args: object, **kwargs: object) -> None:
        calls.append(Path(path))
        if len(calls) == 1:
            raise PermissionError("simulated denied")
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr("esphome_device_builder.helpers.remote_build_cleanup.shutil.rmtree", _flaky)

    deleted = sweep_remote_builds(tmp_path, ttl_seconds=600, in_flight_keys=frozenset(), now=now)
    # One success out of two attempts; the failed subtree still
    # exists, the successful one is gone.
    assert deleted == 1
    assert len(calls) == 2
