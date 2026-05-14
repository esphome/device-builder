"""
Tests for the receiver-side TTL cleanup sweep (issue #106).

Drives the helper directly against real on-disk subtrees + bundle
tarballs constructed under :class:`tmp_path`; the controller's
periodic loop is a thin executor-hop around this function so the
disk-side branches all surface here.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from esphome_device_builder.helpers.remote_build_cleanup import (
    _is_cold,
    _safe_iterdir,
    sweep_remote_builds,
)
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


def test_sweep_prunes_dashboard_parent_with_macos_metadata(tmp_path: Path) -> None:
    """Dashboard_id parent holding only ``.DS_Store`` / ``._*`` still rmdirs."""
    now = 1_000_000.0
    key = RemoteBuildPath(dashboard_id="alpha", device_name="kitchen")
    _populate(tmp_path, key, age_seconds=3600, now=now)
    dashboard_dir = tmp_path / REMOTE_BUILDS_SUBDIR / "alpha"
    (dashboard_dir / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
    (dashboard_dir / "._kitchen").write_bytes(b"AppleDouble")

    sweep_remote_builds(tmp_path, ttl_seconds=600, in_flight_keys=frozenset(), now=now)
    assert not dashboard_dir.exists()


def test_sweep_leaves_macos_metadata_alongside_warm_subtree(tmp_path: Path) -> None:
    """Metadata purge is scoped to metadata-only parents, not non-empty ones."""
    now = 1_000_000.0
    warm = RemoteBuildPath(dashboard_id="alpha", device_name="kitchen")
    _populate(tmp_path, warm, age_seconds=60, now=now)
    dashboard_dir = tmp_path / REMOTE_BUILDS_SUBDIR / "alpha"
    ds_store = dashboard_dir / ".DS_Store"
    apple_double = dashboard_dir / "._kitchen"
    ds_store.write_bytes(b"\x00\x00\x00\x01Bud1")
    apple_double.write_bytes(b"AppleDouble")

    sweep_remote_builds(tmp_path, ttl_seconds=600, in_flight_keys=frozenset(), now=now)
    assert warm.subtree(tmp_path).is_dir()
    assert ds_store.is_file()
    assert apple_double.is_file()


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


def test_sweep_reclaims_cold_orphan_bundle(tmp_path: Path) -> None:
    """An orphan .tar.gz (subtree missing) gets unlinked when cold + not in-flight.

    Models the post-failure state where a previous sweep's
    ``rmtree`` succeeded but the bundle ``unlink`` failed (or
    where an operator hand-deleted the subtree but left the
    tarball, or any other transient failure that left only the
    bundle behind). Without this branch the orphan would
    accumulate forever — the subtree-path of the sweep
    requires ``is_dir()`` and never visits the bundle.
    """
    now = 1_000_000.0
    key = RemoteBuildPath(dashboard_id="alpha", device_name="kitchen")
    bundle = key.bundle(tmp_path)
    bundle.parent.mkdir(parents=True, exist_ok=True)
    bundle.write_bytes(b"orphan bundle bytes")
    age_seconds = 3600
    os.utime(bundle, (now - age_seconds, now - age_seconds))

    sweep_remote_builds(tmp_path, ttl_seconds=600, in_flight_keys=frozenset(), now=now)
    assert not bundle.exists()


def test_sweep_keeps_fresh_orphan_bundle(tmp_path: Path) -> None:
    """A within-TTL orphan bundle survives the sweep."""
    now = 1_000_000.0
    key = RemoteBuildPath(dashboard_id="alpha", device_name="kitchen")
    bundle = key.bundle(tmp_path)
    bundle.parent.mkdir(parents=True, exist_ok=True)
    bundle.write_bytes(b"fresh orphan")
    os.utime(bundle, (now - 60, now - 60))

    sweep_remote_builds(tmp_path, ttl_seconds=600, in_flight_keys=frozenset(), now=now)
    assert bundle.is_file()


def test_sweep_skips_in_flight_orphan_bundle(tmp_path: Path) -> None:
    """An orphan bundle whose key is in-flight is left alone.

    Covers the racy edge: a ``submit_job`` mid-flow has written
    the bundle but hasn't laid down the subtree yet; reclaiming
    the bundle here would yank the input out from under the
    in-flight extract.
    """
    now = 1_000_000.0
    key = RemoteBuildPath(dashboard_id="alpha", device_name="kitchen")
    bundle = key.bundle(tmp_path)
    bundle.parent.mkdir(parents=True, exist_ok=True)
    bundle.write_bytes(b"in-flight bundle")
    os.utime(bundle, (now - 3600, now - 3600))

    sweep_remote_builds(
        tmp_path,
        ttl_seconds=600,
        in_flight_keys=frozenset({key}),
        now=now,
    )
    assert bundle.is_file()


def test_sweep_does_not_unlink_bundle_when_subtree_still_exists(tmp_path: Path) -> None:
    """A paired (subtree+bundle) pair flows through the subtree branch only.

    Defends against the orphan branch firing in the
    paired-cold case: that would double-unlink (subtree-path's
    ``_delete_subtree_and_sibling`` deletes the bundle, and
    then the orphan branch would try to unlink an
    already-deleted file). The sibling-subtree-exists gate
    inside the orphan path keeps the two branches disjoint.
    """
    now = 1_000_000.0
    key = RemoteBuildPath(dashboard_id="alpha", device_name="kitchen")
    _populate(tmp_path, key, age_seconds=3600, now=now)

    # Pre-condition: both subtree and bundle present.
    assert key.subtree(tmp_path).is_dir()
    assert key.bundle(tmp_path).is_file()

    deleted = sweep_remote_builds(tmp_path, ttl_seconds=600, in_flight_keys=frozenset(), now=now)
    assert deleted == 1
    assert not key.subtree(tmp_path).exists()
    assert not key.bundle(tmp_path).exists()


def test_sweep_skips_when_root_itself_is_symlink(tmp_path: Path) -> None:
    """A symlink at the remote-builds root is skipped, not traversed.

    Defense-in-depth: ``root.is_dir()`` follows symlinks; a
    symlink at ``<config_dir>/.esphome/.remote_builds`` would
    otherwise pass the check and the sweep would walk into
    whatever directory the symlink targets, potentially
    reclaiming subtrees outside the canonical layout. The
    explicit ``is_symlink`` skip catches this.
    """
    now = 1_000_000.0
    real_root = tmp_path / "elsewhere"
    real_root.mkdir()
    # Plant a populated subtree at the symlink TARGET to make
    # sure the sweep wouldn't accidentally clean it up.
    (real_root / "alpha").mkdir()
    (real_root / "alpha" / "kitchen").mkdir()
    (real_root / "alpha" / "kitchen" / "important.txt").write_text("hands off")
    age_seconds = 3600
    target_mtime = now - age_seconds
    os.utime(real_root / "alpha" / "kitchen", (target_mtime, target_mtime))

    # Make the remote-builds root a symlink pointing at the
    # populated real_root.
    (tmp_path / ".esphome").mkdir()
    (tmp_path / ".esphome" / ".remote_builds").symlink_to(real_root)

    deleted = sweep_remote_builds(tmp_path, ttl_seconds=600, in_flight_keys=frozenset(), now=now)
    assert deleted == 0
    assert (real_root / "alpha" / "kitchen" / "important.txt").is_file()


def test_sweep_skips_symlink_at_dashboard_level(tmp_path: Path) -> None:
    """A symlink at the ``<dashboard_id>/`` level is left untouched.

    Defense-in-depth: a symlink that resolves to a directory
    elsewhere on the FS would otherwise pass the ``is_dir()``
    gate and reach ``rmtree``, which refuses top-level symlinks
    but emits a warning. The explicit ``is_symlink`` skip
    catches it earlier so the warning never fires AND a
    malicious operator-placed symlink pointing outside the
    canonical layout can't even be considered.
    """
    now = 1_000_000.0
    root = tmp_path / REMOTE_BUILDS_SUBDIR
    root.mkdir(parents=True)
    real_dir = tmp_path / "untouchable"
    real_dir.mkdir()
    (real_dir / "important.txt").write_text("hands off")
    link = root / "alpha"
    link.symlink_to(real_dir)

    sweep_remote_builds(tmp_path, ttl_seconds=600, in_flight_keys=frozenset(), now=now)
    # The symlink stays in place because the dashboard-level
    # ``is_symlink()`` skip in ``sweep_remote_builds`` does
    # ``continue`` before reaching ``_prune_empty_dir``; the
    # load-bearing assertion is that the target's contents are
    # intact (the sweep didn't walk through the symlink and
    # delete anything on the other side).
    assert link.is_symlink()
    assert (real_dir / "important.txt").is_file()


def test_sweep_skips_symlink_at_subtree_level(tmp_path: Path) -> None:
    """A symlink under ``<dashboard_id>/`` is skipped, not deleted-through."""
    now = 1_000_000.0
    dashboard_dir = tmp_path / REMOTE_BUILDS_SUBDIR / "alpha"
    dashboard_dir.mkdir(parents=True)
    real_dir = tmp_path / "untouchable"
    real_dir.mkdir()
    (real_dir / "important.txt").write_text("hands off")
    link = dashboard_dir / "kitchen"
    link.symlink_to(real_dir)

    sweep_remote_builds(tmp_path, ttl_seconds=600, in_flight_keys=frozenset(), now=now)
    assert (real_dir / "important.txt").is_file()


def test_sweep_leaves_bare_dot_tar_gz_alone(tmp_path: Path) -> None:
    """A pathological bare ``.tar.gz`` entry doesn't trip the orphan branch.

    ``device_name = bundle.name[: -len(BUNDLE_SUFFIX)]`` would
    be empty for a file literally named ``.tar.gz``; the
    explicit empty-name short-circuit in
    ``_reclaim_orphan_bundle`` avoids spurious work and the
    dashboard_dir-as-sibling false positive that would otherwise
    follow.
    """
    now = 1_000_000.0
    dashboard_dir = tmp_path / REMOTE_BUILDS_SUBDIR / "alpha"
    dashboard_dir.mkdir(parents=True)
    weird = dashboard_dir / ".tar.gz"
    weird.write_bytes(b"weirdo")
    os.utime(weird, (now - 3600, now - 3600))

    sweep_remote_builds(tmp_path, ttl_seconds=600, in_flight_keys=frozenset(), now=now)
    assert weird.is_file()


def test_safe_iterdir_returns_empty_on_oserror() -> None:
    """``_safe_iterdir`` swallows OSError and returns an empty list.

    The sweep's outer + inner loops both pass this helper's
    return value to ``for ... in ...``; a propagated OSError
    here would unwind the whole sweep and the controller's
    cleanup loop would log the per-cycle exception. Pin the
    log-and-fallthrough contract directly.
    """
    fake_dir = MagicMock(spec=Path)
    fake_dir.iterdir.side_effect = PermissionError("simulated denied")
    assert _safe_iterdir(fake_dir) == []


def test_is_cold_returns_false_on_stat_error() -> None:
    """``_is_cold`` treats a stat failure as "not cold" — skip rather than guess.

    Pins the defensive arm: a stat that raises (broken symlink
    resolution mid-walk, race against rmtree, permission flip)
    returns ``False`` so the sweep doesn't try to delete a
    subtree it can't measure.
    """
    fake_subtree = MagicMock(spec=Path)
    fake_subtree.stat.side_effect = PermissionError("simulated stat denied")
    assert _is_cold(fake_subtree, cutoff=0) is False


def test_sweep_logs_sibling_unlink_failure_but_still_counts_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sibling-bundle unlink raising doesn't unwind the subtree delete.

    Pins the docstring contract: the subtree is the load-bearing
    reclamation; the bundle is a tiny cache file. A failed
    bundle unlink logs at warning but the sweep returns the
    subtree as "deleted" so the operator-facing count reflects
    actual disk reclaimed.
    """
    now = 1_000_000.0
    key = RemoteBuildPath(dashboard_id="alpha", device_name="kitchen")
    _populate(tmp_path, key, age_seconds=3600, now=now)
    real_unlink = Path.unlink

    def _flaky_unlink(self: Path, *args: object, **kwargs: object) -> object:
        if self == key.bundle(tmp_path):
            raise PermissionError("simulated denied")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _flaky_unlink)

    deleted = sweep_remote_builds(tmp_path, ttl_seconds=600, in_flight_keys=frozenset(), now=now)
    assert deleted == 1
    assert not key.subtree(tmp_path).exists()
    # Bundle unlink failed → bundle still on disk; the next
    # sweep will retry it via the orphan-bundle branch.
    assert key.bundle(tmp_path).is_file()


def test_sweep_logs_orphan_unlink_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An orphan bundle whose unlink raises is logged + the sweep continues.

    Pins ``_reclaim_orphan_bundle``'s defensive arm: a
    PermissionError on the unlink doesn't unwind the sweep.
    """
    now = 1_000_000.0
    key = RemoteBuildPath(dashboard_id="alpha", device_name="kitchen")
    bundle = key.bundle(tmp_path)
    bundle.parent.mkdir(parents=True, exist_ok=True)
    bundle.write_bytes(b"orphan")
    os.utime(bundle, (now - 3600, now - 3600))

    real_unlink = Path.unlink

    def _flaky_unlink(self: Path, *args: object, **kwargs: object) -> object:
        if self == bundle:
            raise PermissionError("simulated denied")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _flaky_unlink)

    # Should not raise.
    sweep_remote_builds(tmp_path, ttl_seconds=600, in_flight_keys=frozenset(), now=now)
    assert bundle.is_file()


def test_sweep_logs_macos_metadata_unlink_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failed ``.DS_Store`` unlink + the follow-on rmdir failure both stay debug-logs."""
    now = 1_000_000.0
    dashboard_dir = tmp_path / REMOTE_BUILDS_SUBDIR / "alpha"
    dashboard_dir.mkdir(parents=True)
    ds_store = dashboard_dir / ".DS_Store"
    ds_store.write_bytes(b"junk")

    real_unlink = Path.unlink

    def _flaky_unlink(self: Path, *args: object, **kwargs: object) -> object:
        if self == ds_store:
            raise PermissionError("simulated denied")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _flaky_unlink)

    # Should not raise. Both defensive arms fire: the unlink
    # OSError is logged, then the rmdir OSError (dir still
    # holds the .DS_Store) is logged too.
    sweep_remote_builds(tmp_path, ttl_seconds=600, in_flight_keys=frozenset(), now=now)
    assert dashboard_dir.is_dir()
    assert ds_store.is_file()


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
