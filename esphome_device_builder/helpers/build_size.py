"""Cached, mtime-gated walk of a device's per-build artifact directory.

ESPHome compiles produce a meaty ``.esphome/build/<device>/`` tree —
PlatformIO checkouts, framework toolchain output, intermediate ``.o``
files, the linked binaries. A typical board lands at 50-250 MB, with
heavier configs (BLE-tracker fleets, multi-bus designs) easily past a
gigabyte. The dashboard surfaces this size in the per-device drawer
and as a hidden-by-default table column so a user planning a clean-up
can see at a glance which devices are eating disk.

The walk itself is heavy + I/O-bound — every entry under the tree
needs an ``stat()`` to get its size, and a fleet with dozens of
devices would otherwise burn measurable time on every dashboard
load. We cache the computed total in the per-device metadata sidecar
keyed off a *freshness pair*: ``(dir_mtime, build_info_mtime)``.
Either side moving counts as stale; both halves are needed because
each catches a class of changes the other misses.

Empirical matrix (Python 3.12, macOS APFS) — which signal moves
under each write pattern that ESPHome / PlatformIO actually use::

    case                      | dir       | firmware  | sibling
    --------------------------+-----------+-----------+----------
    truncate + same content   | unchanged | moved     | unchanged
    truncate + diff content   | unchanged | moved     | unchanged
    atomic-replace (rename)   | MOVED     | moved     | unchanged
    add a sibling             | MOVED     | unchanged | unchanged
    remove a sibling          | MOVED     | unchanged | unchanged
    write_file_if_changed=    | unchanged | unchanged | unchanged
    write_file_if_changed≠    | unchanged | unchanged | MOVED

``write_file_if_changed`` (used by ``esphome/writer.py`` for
``build_info.json``, ``main.cpp``, etc.) deliberately skips the
write when content is identical — so a no-op recompile is correctly
a no-op for the cache too. When the compile *does* produce
different output, ``write_file_if_changed`` truncates-and-writes,
which moves the file's own mtime (caught by ``build_info_mtime``)
but leaves the parent dir's mtime alone. PlatformIO's intermediate
work churns siblings, which moves the parent dir's mtime
(caught by ``dir_mtime``) but leaves ``build_info.json`` alone.
Tracking both gives full coverage; tracking either alone has
real holes.

The build path itself comes from the device's ``StorageJSON``
sidecar (written by ESPHome at compile time), via
``resolve_build_dir``. Devices that haven't been compiled yet —
fresh adoption / wizard / on-disk YAML drop — return ``None``;
callers treat that the same as "no cached value, total = 0".
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from esphome.storage_json import StorageJSON

from ..controllers.config import get_device_metadata, set_device_metadata
from .storage_path import resolve_storage_path

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BuildDirSignal:
    """Two-stat freshness signal for a per-device build directory.

    ``dir_mtime`` and ``info_mtime`` are whole-second
    ``int(stat.st_mtime)`` values; ``0`` means that path is
    missing. Each catches a class of changes the other misses
    (see the module docstring's empirical matrix), so the cache
    treats inequality on either half as "stale".
    """

    dir_mtime: int
    info_mtime: int


@dataclass(frozen=True, slots=True)
class BuildSizeRefreshResult:
    """The persisted triple after a successful walk: size + freshness pair.

    ``size_bytes`` is the recursive-walk sum;
    ``signal`` is the freshness pair captured at walk time, used
    to short-circuit the next call when the dir hasn't moved.
    """

    size_bytes: int
    signal: BuildDirSignal


def resolve_build_dir(filename: str) -> Path | None:
    """
    Return the per-device build directory path, or ``None`` if unknown.

    Reads the canonical ``build_path`` off the device's StorageJSON
    sidecar — the same path ``shutil.rmtree``'d by the archive /
    delete flow's ``_wipe_device_build_dir``. ``None`` covers two
    cases: no StorageJSON yet (device hasn't been compiled) and an
    older StorageJSON whose ``build_path`` was never populated.
    Both legitimately mean "we have no build artifacts to size."
    """
    try:
        storage = StorageJSON.load(resolve_storage_path(filename))
    except Exception:
        # ``StorageJSON.load`` returns ``None`` on a missing file
        # but raises on a corrupt one; treat both as "no build
        # artifacts on disk."
        _LOGGER.debug("StorageJSON load failed for %s", filename, exc_info=True)
        return None
    if storage is None or not storage.build_path:
        return None
    return Path(storage.build_path)


def get_build_dir_signal(build_dir: Path) -> BuildDirSignal:
    """
    Return the :class:`BuildDirSignal` freshness pair for *build_dir*.

    Both stats are whole-second ints; ``0`` means that path is
    missing. Tracking *both* because each catches a class of
    changes the other misses — empirically verified:

    - **Dir mtime** moves on entry-set churn (sibling add /
      remove / rename, atomic-replace via ``os.replace``). Misses
      truncate-overwrites of an existing file (no entry change).

    - **build_info.json mtime** moves on every real ESPHome
      recompile because :func:`esphome.writer.write_file_if_changed`
      rewrites it with the new ``config_hash`` / ``esphome_version``.
      Misses PlatformIO's intermediate sibling churn (since
      ``build_info.json`` itself isn't touched on those passes).

    Either side moving means "something real happened," so the
    cache compares both halves of the pair and treats inequality
    on either as stale. ``build_info.json`` is the *preferred*
    signal — it's the one ESPHome guarantees to write per
    successful compile / ``--only-generate`` — but a missing
    file (``mtime == 0``) just means we lean on the dir mtime
    half until ESPHome catches up.
    """
    return BuildDirSignal(
        dir_mtime=get_build_dir_mtime(build_dir),
        info_mtime=get_build_dir_mtime(build_dir / "build_info.json"),
    )


def find_stale_build_dirs(config_dir: Path, filenames: list[str]) -> list[str]:
    """
    Cheap fleet-wide stat: return the subset whose freshness pair moved.

    Run this in one executor job ahead of the heavy walk so the
    thread-pool round-trip overhead stays at a single hop for the
    "is anything actually out of date?" question — if the answer
    is "nothing", we never have to schedule a walk job at all.

    For each *filename*, resolves the build dir via
    ``StorageJSON`` and stats the
    ``(dir_mtime, build_info_mtime)`` pair in whole seconds, then
    compares against the cached pair in the sidecar. The
    filenames where *either* current ≠ cached are returned in the
    same order the caller passed them — including the
    ``current = (0, 0)`` but cached non-zero case, where the
    build dir vanished after the last walk and the worker still
    needs to clear the stale cached values. Devices with no
    StorageJSON / no ``build_path`` are silently skipped —
    pre-first-compile devices have nothing to walk in the first
    place, so there's no cache state to clear either.

    Used at startup to catch CLI-driven compiles that ran while
    the dashboard wasn't watching, plus any other fleet-refresh
    trigger; the per-device hot path
    (``refresh_build_size_if_stale``) covers a single device on
    demand.
    """
    stale: list[str] = []
    # Load the metadata file once for the whole sweep — the
    # per-device entries live in a single JSON blob, and
    # ``get_device_metadata`` would re-parse it from disk N times
    # in the loop otherwise. The fleet sweep is the cheap-cost
    # path the cache exists to keep cheap; one read is correct.
    full_metadata = _load_full_metadata(config_dir)
    for filename in filenames:
        build_dir = resolve_build_dir(filename)
        if build_dir is None:
            continue
        current = get_build_dir_signal(build_dir)
        entry = full_metadata.get(filename, {})
        cached = BuildDirSignal(
            dir_mtime=coerce_sidecar_int(entry.get("build_size_dir_mtime")),
            info_mtime=coerce_sidecar_int(entry.get("build_size_info_mtime")),
        )
        # Equality covers every case correctly: dir-gone +
        # cache-empty short-circuits ((0, 0) == (0, 0)),
        # dir-gone + cache-populated falls through to walk
        # (which clears), dir-present + cache-stale falls
        # through to walk. We *don't* short-circuit on
        # "current is empty" — that case used to be skipped
        # but it leaked stale non-zero cache state forever
        # when a user manually wiped a build dir.
        if current != cached:
            stale.append(filename)
    return stale


def _load_full_metadata(config_dir: Path) -> dict[str, dict]:
    """Read the per-device metadata file once. Returns ``{}`` on any error.

    Used by :func:`find_stale_build_dirs` to amortize the JSON
    parse across an N-device fleet sweep into a single read.
    The format is a plain dict-of-dicts keyed on the device's
    YAML filename; missing entries / wrong shapes return ``{}``
    so callers can treat them as "no cached data."
    """
    # Local import flags this as an intentional reach into
    # ``controllers.config``'s underscore-prefixed surface (so a
    # future grep for ``_load_metadata`` finds the call site
    # alongside the explanation). Module-level ``controllers.config``
    # is already imported above for the public helpers, so this
    # isn't a layering boundary — just a "yes, we know we're
    # touching the private one, here's why" marker.
    from ..controllers.config import _load_metadata  # noqa: PLC0415

    try:
        raw = _load_metadata(config_dir)
    except Exception:
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, dict)}


def refresh_build_size_if_stale(
    config_dir: Path,
    filename: str,
) -> BuildSizeRefreshResult | None:
    """
    Refresh the cached size when the build dir's freshness pair moved.

    Bundles the whole refresh into one synchronous unit so callers
    can hand it to a single ``run_in_executor`` rather than
    chaining four separate executor hops (sidecar-read / resolve /
    stat / walk / sidecar-write). Returns ``None`` when the
    cached pair was already fresh — the steady-state
    dashboard-poll path, where we want to skip every executor
    handoff we can. When stale, returns the freshly-persisted
    :class:`BuildSizeRefreshResult` (size + freshness pair); the
    caller uses the non-None return as the signal to reload /
    fire ``DEVICE_UPDATED``.

    Sequencing is cheap-first-then-heavy:
    ``get_device_metadata`` → JSON read on a small file,
    ``resolve_build_dir`` → small StorageJSON read,
    ``get_build_dir_signal`` → two ``stat()`` calls,
    ``compute_build_dir_size`` → the recursive walk we're trying
    to avoid. The walk only runs when *either* half of the
    freshness pair moved; the dashboard-restart cold path takes
    the walk once per device and every subsequent poll
    short-circuits on the cached pair match.
    """
    build_dir = resolve_build_dir(filename)
    if build_dir is None:
        return None
    current = get_build_dir_signal(build_dir)
    md = get_device_metadata(config_dir, filename)
    cached = BuildDirSignal(
        dir_mtime=coerce_sidecar_int(md.get("build_size_dir_mtime")),
        info_mtime=coerce_sidecar_int(md.get("build_size_info_mtime")),
    )
    # Pure equality check across the whole pair. Crucially this
    # short-circuits the "build dir is missing AND we never had
    # one cached" case (signal(0, 0) == signal(0, 0) → fresh →
    # return None); without that we'd walk the missing path on
    # every poll and ``set_device_metadata`` would re-clear
    # three fields that are already absent, with the non-None
    # return retriggering a scanner.reload each time. The "dir
    # vanished but cache was populated" case still falls through
    # to walk so the cache gets cleared exactly once.
    if current == cached:
        return None
    size = compute_build_dir_size(build_dir)
    set_device_metadata(
        config_dir,
        filename,
        build_size_bytes=size,
        build_size_dir_mtime=current.dir_mtime,
        build_size_info_mtime=current.info_mtime,
    )
    return BuildSizeRefreshResult(size_bytes=size, signal=current)


def coerce_sidecar_int(value: object) -> int:
    """Coerce a cached sidecar value to ``int``; ``0`` on any failure.

    Same defensive shape ``_resolve_device_metadata`` uses for the
    cached size — a hand-edited or partially-written sidecar
    could land here with a non-numeric value (``None``, an
    object, a decimal-string like ``"12.7"``). Falling back to
    ``0`` matches the "never walked" sentinel and lets the next
    refresh repopulate from the build dir.
    """
    # Narrow before ``int(...)`` so mypy can resolve the overload —
    # JSON-decoded sidecars realistically carry only these scalar
    # shapes; lists / dicts / arbitrary objects fall straight to
    # the ``0`` sentinel without an exception round-trip.
    if not isinstance(value, (int, float, str, bytes)):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def get_build_dir_mtime(path: Path) -> int:
    """
    Stat *path*'s mtime in whole seconds. ``0`` when the path is missing.

    Used for both halves of the freshness pair —
    :func:`get_build_dir_signal` calls it on the build dir
    itself (entry-set churn) and on ``build_info.json``
    (ESPHome's per-recompile rewrite). Same cross-filesystem
    rationale either way.

    We deliberately truncate to whole seconds rather than carrying
    ``st_mtime``'s native float precision: filesystems that don't
    support sub-second mtimes (FAT32 on a USB-mounted backup, some
    NFS / CIFS shares, older ext3) round to the nearest second on
    write. A cached fractional value from one filesystem would
    then never compare equal to the truncated re-stat after a
    cross-mount move, so the dashboard would walk the tree on
    every poll. Whole seconds give us the cross-filesystem safety
    at the cost of one false-negative re-walk per second of clock
    drift around a real change — entirely fine for a heavy-I/O
    cache primarily meant to skip steady-state polls.

    Returning ``0`` for a missing path short-circuits the cache
    (any non-zero cached mtime wouldn't match) and naturally
    drives the cached size back to zero on the next refresh.
    """
    try:
        return int(path.stat().st_mtime)
    except OSError:
        return 0


def compute_build_dir_size(build_dir: Path) -> int:
    """
    Sum every regular-file size under *build_dir*, recursive.

    Heavy + I/O-bound — caller is responsible for gating this
    behind an mtime check (``get_build_dir_mtime`` above) and for
    running it off the event loop (e.g. via
    ``asyncio.to_thread``). Returns ``0`` when the dir is missing
    or empty. Per-entry ``OSError`` (e.g. a vanishing file mid-
    walk during a concurrent compile) is swallowed so the total
    reflects what we could see rather than failing the whole
    operation.

    Walks via ``os.walk()`` rather than ``Path.rglob`` — the
    former delegates to ``os.scandir()`` since Python 3.5, which
    gets cached ``d_type`` from ``readdir()`` so we don't pay a
    syscall just to know "is this a file or a dir". ``rglob``
    allocates a ``Path`` per entry and re-stats for ``is_file()``,
    which roughly doubles the syscall count on big build trees
    (PlatformIO checkouts can be 10k+ files).
    """
    total = 0
    # ``onerror`` swallows top-level failures (missing dir,
    # permission denied at root). Per-file ``getsize`` errors
    # are caught individually below so a vanishing file mid-walk
    # doesn't kill the whole operation.
    for dirpath, _dirnames, filenames in os.walk(build_dir, onerror=lambda _e: None):
        for filename in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, filename))
            except OSError:
                # File vanished mid-walk (concurrent compile
                # cleanup, symlink target removed, …). Skip and
                # keep going — a partial total is better than
                # no total.
                continue
    return total
