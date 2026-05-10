"""
Receiver-side TTL cleanup sweep for the remote-build subtree.

Phase 6c of issue #106. Disk-side counterpart to the periodic
loop in :class:`RemoteBuildController`: walks every
``<dashboard_id>/<device_name>/`` subtree under the remote-builds
root, deletes the ones whose modification time is older than
the operator-configured TTL AND aren't tracked by an in-flight
:class:`FirmwareJob`. The path layout lives in a single source
of truth in :mod:`helpers.remote_build_layout`; this module is
just the walk + delete logic.

Why directory mtime tracks "last submitted-to": upstream
:func:`esphome.bundle.prepare_bundle_for_compile` wipes the
subtree contents and re-extracts on every submission, so the
subtree's own ``st_mtime`` bumps each time. Compile output
writing inside the subtree (PIO build cache under ``.pioenvs/``)
also bumps the parent's mtime through the same syscall path, so
a running compile keeps its subtree warm before the next submit
even lands. The in-flight gate is still load-bearing for the
QUEUED case (waiting in the receiver's queue before
``JOB_STARTED`` fires) and for the brief gap between a job
completing and the next submission.

Empty ``<dashboard_id>/`` parents are pruned after the subtree
sweep so an offloader that's been removed entirely doesn't
leave a permanent empty directory behind.

Best-effort: per-subtree exceptions (permission denied, races
against a concurrent submit) get logged and the walk continues.
A single bad subtree doesn't kill the sweep for everything else.
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

from .remote_build_layout import REMOTE_BUILDS_SUBDIR, RemoteBuildPath

_LOGGER = logging.getLogger(__name__)


def sweep_remote_builds(
    config_dir: Path,
    *,
    ttl_seconds: float,
    in_flight_keys: frozenset[RemoteBuildPath],
    now: float | None = None,
) -> int:
    """Delete cold remote-build subtrees under *config_dir*.

    Synchronous; designed to run inside an executor (the
    filesystem walk + ``shutil.rmtree`` are blocking syscalls).

    Args:
        config_dir: The receiver's ``CORE.config_dir`` — the
            sweep operates on
            ``config_dir / REMOTE_BUILDS_SUBDIR``.
        ttl_seconds: Delete every subtree whose ``st_mtime`` is
            older than ``now - ttl_seconds``. Values <= 0 are
            treated as "delete everything not in-flight"; the
            settings layer caps inputs so a zero TTL only
            reaches here on an operator override.
        in_flight_keys: :class:`RemoteBuildPath` keys whose
            subtrees the receiver is currently compiling or has
            queued; the controller derives this from its
            firmware queue via
            :func:`helpers.remote_build_layout.parse_from_configuration`.
        now: Optional override for "current time"; tests pin a
            specific value so the mtime comparison is
            deterministic.

    Returns:
        Number of subtrees deleted. Useful for the caller's log
        line so operators can see the cleanup running.
    """
    if now is None:
        now = time.time()
    cutoff = now - ttl_seconds
    root = config_dir / REMOTE_BUILDS_SUBDIR
    if not root.is_dir():
        return 0

    deleted = 0
    for dashboard_dir in _safe_iterdir(root):
        if not dashboard_dir.is_dir():
            _LOGGER.debug(
                "remote-build cleanup: skipping non-directory under %s: %s",
                root,
                dashboard_dir,
            )
            continue
        for entry in _safe_iterdir(dashboard_dir):
            if not entry.is_dir():
                # Bare sibling tarballs are paired with their
                # subtree below; an orphan tarball (subtree
                # already deleted but the .tar.gz survived a
                # previous half-failed sweep) gets reclaimed
                # when its sibling-named subtree next appears
                # OR by an offloader re-submitting the same
                # device_name (which overwrites it).
                continue
            key = RemoteBuildPath(dashboard_id=dashboard_dir.name, device_name=entry.name)
            if key in in_flight_keys:
                _LOGGER.debug("remote-build cleanup: skipping in-flight %s", key)
                continue
            if not _is_cold(entry, cutoff):
                continue
            if _delete_subtree_and_sibling(key, config_dir):
                deleted += 1
        # An offloader that was paired once and never came back
        # leaves an otherwise-permanent empty dashboard_id dir;
        # prune here so the filesystem stays tidy without a
        # separate housekeeping pass.
        _prune_empty_dir(dashboard_dir)
    return deleted


def _safe_iterdir(directory: Path) -> list[Path]:
    """Return entries under *directory*, or empty on error."""
    try:
        return list(directory.iterdir())
    except OSError as exc:
        _LOGGER.debug("remote-build cleanup: iterdir(%s) failed: %s", directory, exc)
        return []


def _is_cold(subtree: Path, cutoff: float) -> bool:
    """Return ``True`` when *subtree*'s mtime is older than *cutoff*.

    On stat failure (concurrent rmtree race, broken symlink,
    permission denied) log + treat as "not cold" so the sweep
    doesn't try to delete a subtree it can't measure.
    """
    try:
        return subtree.stat().st_mtime < cutoff
    except OSError as exc:
        _LOGGER.debug("remote-build cleanup: stat(%s) failed: %s", subtree, exc)
        return False


def _delete_subtree_and_sibling(key: RemoteBuildPath, config_dir: Path) -> bool:
    """Delete *key*'s subtree + its sibling bundle tarball.

    Returns ``True`` when the subtree was deleted (regardless
    of whether the sibling tarball delete succeeded — the
    subtree is the load-bearing reclamation; the tarball is a
    tiny cache file). Both deletes are guarded against
    :class:`OSError` so a single bad subtree doesn't poison the
    rest of the sweep.
    """
    subtree = key.subtree(config_dir)
    bundle = key.bundle(config_dir)
    try:
        shutil.rmtree(subtree)
    except OSError as exc:
        _LOGGER.warning("remote-build cleanup: rmtree(%s) failed: %s", subtree, exc)
        return False
    try:
        bundle.unlink(missing_ok=True)
    except OSError as exc:
        _LOGGER.warning("remote-build cleanup: unlink(%s) failed: %s", bundle, exc)
    _LOGGER.info("remote-build cleanup: removed cold subtree %s", subtree)
    return True


def _prune_empty_dir(directory: Path) -> None:
    """Remove *directory* if empty; debug-log + continue otherwise."""
    try:
        directory.rmdir()
    except OSError as exc:
        _LOGGER.debug("remote-build cleanup: rmdir(%s) skipped: %s", directory, exc)
