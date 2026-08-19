"""
Receiver-side TTL cleanup sweep for the remote-build subtree.

Disk-side counterpart to the periodic loop in
:class:`ReceiverController`: walks every
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

An offloader's shared build data dir
(``<data_dir>/.remote_builds/<dashboard_id>/.esphome``) is
reclaimed once nothing remains under its ``<dashboard_id>/``
and no job targeting it is in flight; empty parents are pruned
on both roots.

Best-effort: per-subtree exceptions (permission denied, races
against a concurrent submit) get logged and the walk continues.
A single bad subtree doesn't kill the sweep for everything else.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from esphome.helpers import rmtree

from .remote_build_layout import (
    BUNDLE_SUFFIX,
    DATA_DIR_NAME,
    REMOTE_BUILDS_NAME,
    REMOTE_BUILDS_SUBDIR,
    VENVS_NAME,
    RemoteBuildPath,
    per_dashboard_data_dir,
)

_LOGGER = logging.getLogger(__name__)


def sweep_remote_builds(
    config_dir: Path,
    *,
    data_dir: Path,
    ttl_seconds: float,
    in_flight_keys: frozenset[RemoteBuildPath],
    now: float | None = None,
) -> int:
    """Delete cold remote-build subtrees under *config_dir*.

    Synchronous; designed to run inside an executor (the
    filesystem walk + ``rmtree`` are blocking syscalls).

    Args:
        config_dir: The receiver's ``CORE.config_dir`` — the
            sweep operates on
            ``config_dir / REMOTE_BUILDS_SUBDIR``.
        data_dir: The receiver's ``CORE.data_dir``; holds each
            offloader's shared build data dir, reclaimed once
            nothing under its ``<dashboard_id>/`` remains.
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
    # Skip if the root itself is a symlink — ``is_dir()`` would
    # follow it and the sweep would walk into whatever directory
    # the symlink targets, potentially deleting subtrees outside
    # the canonical layout. The canonical writer (submit_job)
    # creates this root as a real directory; a symlink here is
    # operator-or-attacker-placed and outside trust scope.
    # Defense-in-depth matching the symlink skips at the
    # dashboard_dir and entry levels below.
    if root.is_symlink():
        return 0

    # Split-root deployments (HA add-on, ``ESPHOME_DATA_DIR``) hold the
    # data dir under a second root; walk the union so a dashboard whose
    # config-root dir was pruned on an earlier sweep is still reclaimed.
    data_root = data_dir / REMOTE_BUILDS_NAME
    in_flight_dir_ids = frozenset(key.dir_id for key in in_flight_keys)
    deleted = 0
    for name in _dashboard_names(root, data_root):
        dashboard_dir = root / name
        # Symlinks at any level are skipped outright. ``is_dir()``
        # follows symlinks by default, so a symlink to a directory
        # would pass the next check; ``rmtree`` on that symlink
        # would then refuse (top-level symlink) and emit a warning.
        # Refusing the symlink up-front keeps the sweep silent on
        # foreign filesystem shapes and provides defense-in-depth
        # against an operator (or attacker with write access to
        # the remote-builds root) placing a symlink that points
        # outside the canonical subtree.
        if dashboard_dir.is_symlink() or (dashboard_dir.exists() and not dashboard_dir.is_dir()):
            _LOGGER.debug(
                "remote-build cleanup: skipping non-directory under %s: %s",
                root,
                dashboard_dir,
            )
            continue
        if dashboard_dir.is_dir():
            deleted += _sweep_dashboard_dir(dashboard_dir, config_dir, in_flight_keys, cutoff)
        if name in in_flight_dir_ids or _holds_live_entries(dashboard_dir):
            continue
        # Nothing references this offloader any more: reclaim its
        # build data dir, then prune the empty parents on both roots.
        deleted += _reclaim_dashboard_data_dir(name, data_dir)
        _prune_empty_dir(dashboard_dir)
        _prune_empty_dir(data_root / name)
    return deleted


def _dashboard_names(*roots: Path) -> set[str]:
    """Return the ``<dashboard_id>`` entry names across *roots*, minus the venv cache."""
    names: set[str] = set()
    for root in roots:
        if root.is_symlink() or not root.is_dir():
            continue
        names.update(entry.name for entry in _safe_iterdir(root))
    return {name for name in names if name.casefold() != VENVS_NAME}


def _holds_live_entries(dashboard_dir: Path) -> bool:
    """Return ``True`` while anything but the data dir or macOS metadata sits in *dashboard_dir*."""
    return any(
        entry.name.casefold() != DATA_DIR_NAME and not _is_macos_metadata(entry.name)
        for entry in _safe_iterdir(dashboard_dir)
    )


def _reclaim_dashboard_data_dir(dir_id: str, data_dir: Path) -> int:
    """Delete *dir_id*'s shared build data dir; return the delete count."""
    target = per_dashboard_data_dir(data_dir, dir_id)
    if target.parent.is_symlink() or target.is_symlink() or not target.is_dir():
        return 0
    try:
        rmtree(target)
    except OSError as exc:
        _LOGGER.warning("remote-build cleanup: rmtree(%s) failed: %s", target, exc)
        return 0
    _LOGGER.info("remote-build cleanup: removed idle build data dir %s", target)
    return 1


def _sweep_dashboard_dir(
    dashboard_dir: Path,
    config_dir: Path,
    in_flight_keys: frozenset[RemoteBuildPath],
    cutoff: float,
) -> int:
    """Delete cold device subtrees + orphan bundles under *dashboard_dir*; return the count."""
    deleted = 0
    for entry in _safe_iterdir(dashboard_dir):
        # Standalone-install sibling of the device subtrees, not one of them.
        if entry.name.casefold() == DATA_DIR_NAME:
            continue
        if entry.is_symlink():
            _LOGGER.debug("remote-build cleanup: skipping symlink %s", entry)
            continue
        if entry.is_dir():
            key = RemoteBuildPath(dashboard_id=dashboard_dir.name, device_name=entry.name)
            if key in in_flight_keys:
                _LOGGER.debug("remote-build cleanup: skipping in-flight %s", key)
                continue
            if not _is_cold(entry, cutoff):
                continue
            if _delete_subtree_and_sibling(key, config_dir):
                deleted += 1
        elif entry.is_file() and entry.name.endswith(BUNDLE_SUFFIX):
            # Orphan bundle path: a ``.tar.gz`` whose sibling
            # subtree is missing. Happens when the previous
            # sweep's ``rmtree`` succeeded but the ``unlink``
            # failed (logged as warning, sweep continued),
            # when an operator hand-deleted the subtree, or
            # any other transient failure that left only the
            # tarball behind. Without this branch the orphan
            # would accumulate forever — the subtree-path
            # above never visits it because that branch
            # requires ``is_dir()``. Pair with the same
            # in-flight + cold gates the subtree path uses.
            _reclaim_orphan_bundle(entry, dashboard_dir, in_flight_keys, cutoff)
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
        # Via esphome's wrapper so Windows-side read-only files
        # (PIO compile cache, git pack files in cached venvs)
        # get a ``chmod +w`` retry before the OSError propagates.
        rmtree(subtree)
    except OSError as exc:
        _LOGGER.warning("remote-build cleanup: rmtree(%s) failed: %s", subtree, exc)
        return False
    try:
        bundle.unlink(missing_ok=True)
    except OSError as exc:
        _LOGGER.warning("remote-build cleanup: unlink(%s) failed: %s", bundle, exc)
    _LOGGER.info("remote-build cleanup: removed cold subtree %s", subtree)
    return True


def _reclaim_orphan_bundle(
    bundle: Path,
    dashboard_dir: Path,
    in_flight_keys: frozenset[RemoteBuildPath],
    cutoff: float,
) -> None:
    """Unlink *bundle* when its sibling subtree is missing + it's cold + not in-flight.

    The "sibling subtree missing" gate is load-bearing: when
    both exist the subtree-path branch above already handles
    the bundle delete via :func:`_delete_subtree_and_sibling`,
    so firing here would double-unlink. Restricting this branch
    to true orphans keeps the two paths disjoint.

    The in-flight gate covers the racy edge where a
    ``submit_job`` mid-flow has written the bundle but hasn't
    laid down the subtree yet; the sweep shouldn't reclaim the
    bundle out from under the in-flight extract. The cold gate
    avoids reclaiming a bundle whose subtree was just deleted
    out-of-band and is about to be re-extracted on the next
    submit.
    """
    device_name = bundle.name[: -len(BUNDLE_SUFFIX)]
    # Empty device_name (a bare ``.tar.gz`` entry — pathological,
    # shouldn't happen via the writer) lookups the dashboard_dir
    # itself, which always exists, so this naturally falls
    # through the "sibling exists" gate below and skips. Just
    # short-circuit explicitly to avoid the spurious
    # ``RemoteBuildPath(..., device_name="")`` allocation.
    if not device_name:
        return
    sibling_subtree = dashboard_dir / device_name
    # Treat anything at the sibling position — real dir, real
    # file, broken symlink, live symlink to anywhere — as a
    # signal that this bundle is NOT an orphan we should
    # reclaim. ``exists`` covers real entries (following live
    # symlinks); the explicit ``is_symlink`` arm covers broken
    # symlinks too (``exists`` returns False for those). The
    # safe stance is "leak the tarball" rather than "make a
    # delete decision based on a symlink we don't control":
    # the canonical writer never creates symlinks under the
    # remote-builds root, so anything at this position is
    # operator-or-attacker-placed and outside our trust scope.
    if sibling_subtree.exists() or sibling_subtree.is_symlink():
        return
    key = RemoteBuildPath(dashboard_id=dashboard_dir.name, device_name=device_name)
    if key in in_flight_keys:
        _LOGGER.debug("remote-build cleanup: skipping in-flight orphan bundle %s", bundle)
        return
    if not _is_cold(bundle, cutoff):
        return
    try:
        bundle.unlink()
    except OSError as exc:
        _LOGGER.warning("remote-build cleanup: unlink orphan bundle(%s) failed: %s", bundle, exc)
        return
    _LOGGER.info("remote-build cleanup: removed orphan bundle %s", bundle)


def _is_macos_metadata(name: str) -> bool:
    """Return ``True`` for Finder droppings (``.DS_Store``, AppleDouble ``._*``)."""
    return name == ".DS_Store" or name.startswith("._")


def _prune_empty_dir(directory: Path) -> None:
    """Remove *directory* when empty or holding only macOS metadata."""
    if directory.is_symlink() or not directory.is_dir():
        return
    metadata: list[Path] = []
    for entry in _safe_iterdir(directory):
        if entry.is_symlink() or not _is_macos_metadata(entry.name):
            return
        metadata.append(entry)
    for entry in metadata:
        try:
            entry.unlink()
        except OSError as exc:
            _LOGGER.debug("remote-build cleanup: unlink macOS metadata(%s) failed: %s", entry, exc)
    try:
        directory.rmdir()
    except OSError as exc:
        _LOGGER.debug("remote-build cleanup: rmdir(%s) skipped: %s", directory, exc)
