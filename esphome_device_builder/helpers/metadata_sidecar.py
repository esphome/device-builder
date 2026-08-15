"""Metadata sidecar (.device-builder.json) — atomic RMW persistence."""

from __future__ import annotations

import logging
import os
import stat
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:
    import fcntl

    _HAS_FCNTL = True
except ImportError:  # pragma: no cover — Windows path
    _HAS_FCNTL = False

from .atomic_io import atomic_write, read_bytes_with_retry, replace_with_retry
from .json import JSONDecodeError, dumps_indent, loads

_LOGGER = logging.getLogger(__name__)

_METADATA_FILE = ".device-builder.json"
_METADATA_CORRUPT_FILE = ".device-builder.json.corrupt"
# Original ``.corrupt`` plus at most this many timestamped siblings;
# repeat incidents drop the oldest sibling instead of growing without
# bound in the user-browsable config dir.
_MAX_CORRUPT_SIBLINGS = 3
# Separate sibling file for the flock — ``_save_metadata`` swaps
# ``_METADATA_FILE``'s inode via ``Path.replace`` mid-transaction,
# which would yank the lock out from under any holder.
_METADATA_LOCK_FILE = ".device-builder.json.lock"

# Several controllers (firmware queue, device CRUD, preferences, IP
# cache) all RMW this file from the executor pool. Without serialisation
# two writers landing in the same window lose each other's updates.
# Plain (non-reentrant) ``Lock`` is intentional: nested
# ``metadata_transaction`` calls on the same thread are unsafe even
# under an ``RLock`` because each call does its own load/save, so
# the inner write is overwritten by the outer write at the outer's
# exit. The deadlock on attempted re-entry is the loud failure;
# silently losing updates would be worse. See the docstring below.
_METADATA_LOCK = threading.Lock()


@contextmanager
def metadata_transaction(config_dir: Path) -> Iterator[dict[str, Any]]:
    """
    Atomic read-modify-write context for the metadata sidecar.

    Yields the current metadata dict. Serialised within the
    process by ``_METADATA_LOCK`` and across processes by an
    ``fcntl.flock`` on the sibling lock file — needed for the HA
    addon multi-flavor shape where Prod/Beta/DEV share
    ``/config/esphome``. Exceptions inside the block skip the
    save, and so does a corrupt sidecar that could not be
    quarantined. The per-process lock is non-reentrant; nested calls
    deadlock by design (each call loads its own snapshot, so
    nesting would clobber the inner write at the outer's exit).
    Windows / no-fcntl degrades to per-process only.
    """
    with _METADATA_LOCK:
        if not _HAS_FCNTL:
            data, safe = _load_metadata_guarded(config_dir, quarantine=True)
            before = dumps_indent(data)
            yield data
            _finish_transaction(config_dir, data, before, safe=safe)
            return
        lock_path = config_dir / _METADATA_LOCK_FILE
        with open(lock_path, "a+", encoding="utf-8", opener=_open_metadata_lock_file) as lock_fh:
            # Defense in depth: O_NOFOLLOW rejects symlinks, but a
            # FIFO planted at the lock path would block every
            # transaction on ``open(..., "a+")``. Match the
            # ``_ensure_single_execution`` shape — refuse anything
            # that isn't a regular file.
            st = os.fstat(lock_fh.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise OSError(f"Lock file {lock_path} is not a regular file (mode={st.st_mode:o})")
            # Blocking LOCK_EX (not LOCK_NB like the startup
            # lock) — a transient WS-command race should queue,
            # not fail.
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            data, safe = _load_metadata_guarded(config_dir, quarantine=True)
            before = dumps_indent(data)
            yield data
            _finish_transaction(config_dir, data, before, safe=safe)


def _open_metadata_lock_file(path: str, flags: int) -> int:
    """``open()`` opener that adds ``O_NOFOLLOW`` to reject symlinks."""
    return os.open(path, flags | os.O_NOFOLLOW, 0o644)


def _load_metadata(config_dir: Path) -> dict[str, Any]:
    """Lock-free pure read of the sidecar dict; corrupt or missing content returns ``{}``."""
    return _load_metadata_guarded(config_dir, quarantine=False)[0]


def _load_metadata_guarded(config_dir: Path, *, quarantine: bool) -> tuple[dict[str, Any], bool]:
    """Load plus a write-back-safe flag: ``False`` while corrupt bytes remain in place."""
    path = config_dir / _METADATA_FILE
    try:
        # orjson decodes bytes directly, so skip the read_text → encode
        # round-trip. JSONDecodeError is a subclass of ValueError.
        # read_bytes_with_retry rides out a Windows sharing-violation race
        # against a concurrent ``_save_metadata`` replace — the read is
        # lock-free, so it can open the file mid-rename.
        data = loads(read_bytes_with_retry(path))
    except FileNotFoundError:
        return {}, True
    except JSONDecodeError as err:
        return {}, _handle_corrupt_sidecar(path, str(err), quarantine=quarantine)
    if isinstance(data, dict):
        return data, True
    return {}, _handle_corrupt_sidecar(
        path, f"top-level {type(data).__name__} is not an object", quarantine=quarantine
    )


def _handle_corrupt_sidecar(path: Path, reason: str, *, quarantine: bool) -> bool:
    _LOGGER.warning("Metadata sidecar %s is unparsable (%s)", path, reason)
    if not quarantine:
        return False
    corrupt_path = path.with_name(_METADATA_CORRUPT_FILE)
    if corrupt_path.exists():
        # An earlier incident's copy holds the richest bytes (the
        # regenerated sidecar starts sparse) — never clobber it.
        corrupt_path = corrupt_path.with_name(f"{_METADATA_CORRUPT_FILE}.{time.time_ns()}")
    try:
        replace_with_retry(path, corrupt_path)
    except FileNotFoundError:
        return True  # another process already moved it aside (no-flock platforms)
    except OSError as rename_err:
        _LOGGER.warning("Could not move corrupt metadata sidecar aside: %s", rename_err)
        return False
    _LOGGER.warning("Moved corrupt metadata sidecar to %s; starting fresh", corrupt_path)
    _prune_corrupt_siblings(path, corrupt_path)
    return True


def _prune_corrupt_siblings(path: Path, fresh: Path) -> None:
    # ``fresh`` is exempt regardless of its stamp — ``time.time_ns`` is
    # wall-clock, and a pre-NTP boot or backwards correction can stamp
    # the copy just written below its older siblings.
    # ``isdecimal`` (not ``isdigit``) — the latter admits codepoints
    # like superscripts that ``int()`` rejects.
    siblings = sorted(
        (p for p in path.parent.glob(f"{_METADATA_CORRUPT_FILE}.*") if p.suffix[1:].isdecimal()),
        key=lambda p: int(p.suffix[1:]),
    )
    keep = set(siblings[-_MAX_CORRUPT_SIBLINGS:])
    for stale in siblings:
        if stale in keep or stale == fresh:
            continue
        try:
            stale.unlink()
        except OSError as err:
            _LOGGER.debug("Could not prune corrupt sidecar copy %s: %s", stale, err)


def _finish_transaction(
    config_dir: Path, data: dict[str, Any], before: bytes, *, safe: bool
) -> None:
    if safe:
        _save_metadata_if_changed(config_dir, data, before)
        return
    _LOGGER.warning("Discarding metadata write-back; the corrupt sidecar is still in place")


def _save_metadata(config_dir: Path, data: dict[str, Any]) -> None:
    # Atomic so lock-free readers never observe a partial write.
    # ``dumps_indent`` yields bytes; the on-disk file stays readable / diffable.
    atomic_write(config_dir / _METADATA_FILE, dumps_indent(data))


def _save_metadata_if_changed(config_dir: Path, data: dict[str, Any], before: bytes) -> None:
    # Byte-compare against the serialized pre-image: no-op transactions
    # (migration already ran, entry absent, read-only probe) must not
    # churn the sidecar every boot, and serialization equality is
    # exactly "would this write change the file".
    serialized = dumps_indent(data)
    if serialized != before:
        atomic_write(config_dir / _METADATA_FILE, serialized)
