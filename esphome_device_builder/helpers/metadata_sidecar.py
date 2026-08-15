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
    save. The per-process lock is non-reentrant; nested calls
    deadlock by design (each call loads its own snapshot, so
    nesting would clobber the inner write at the outer's exit).
    Windows / no-fcntl degrades to per-process only.
    """
    with _METADATA_LOCK:
        if not _HAS_FCNTL:
            data = _load_metadata(config_dir, quarantine=True)
            before = dumps_indent(data)
            yield data
            _save_metadata_if_changed(config_dir, data, before)
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
            data = _load_metadata(config_dir, quarantine=True)
            before = dumps_indent(data)
            yield data
            _save_metadata_if_changed(config_dir, data, before)


def _open_metadata_lock_file(path: str, flags: int) -> int:
    """``open()`` opener that adds ``O_NOFOLLOW`` to reject symlinks."""
    return os.open(path, flags | os.O_NOFOLLOW, 0o644)


def _load_metadata(config_dir: Path, *, quarantine: bool = False) -> dict[str, Any]:
    """
    Load the sidecar dict; corrupt content returns ``{}``.

    ``quarantine=True`` (only safe under ``metadata_transaction``'s
    locks) side-renames corrupt content to ``.corrupt`` so the
    write-back can't destroy it; lock-free readers must stay pure —
    a stale decode racing a rewrite would quarantine the fresh file.
    """
    path = config_dir / _METADATA_FILE
    try:
        # orjson decodes bytes directly, so skip the read_text → encode
        # round-trip. JSONDecodeError is a subclass of ValueError.
        # read_bytes_with_retry rides out a Windows sharing-violation race
        # against a concurrent ``_save_metadata`` replace — the read is
        # lock-free, so it can open the file mid-rename.
        data = loads(read_bytes_with_retry(path))
    except FileNotFoundError:
        return {}
    except JSONDecodeError as err:
        _handle_corrupt_sidecar(path, str(err), quarantine=quarantine)
        return {}
    if isinstance(data, dict):
        return data
    _handle_corrupt_sidecar(
        path, f"top-level {type(data).__name__} is not an object", quarantine=quarantine
    )
    return {}


def _handle_corrupt_sidecar(path: Path, reason: str, *, quarantine: bool) -> None:
    if not quarantine:
        _LOGGER.warning("Metadata sidecar %s is unparsable (%s)", path, reason)
        return
    corrupt_path = path.with_name(_METADATA_CORRUPT_FILE)
    if corrupt_path.exists():
        # An earlier incident's copy holds the richest bytes (the
        # regenerated sidecar starts sparse) — never clobber it.
        corrupt_path = corrupt_path.with_name(f"{_METADATA_CORRUPT_FILE}.{time.time_ns()}")
    _LOGGER.warning(
        "Metadata sidecar %s is unparsable (%s); moving it to %s and starting fresh",
        path,
        reason,
        corrupt_path,
    )
    try:
        replace_with_retry(path, corrupt_path)
    except FileNotFoundError:
        pass  # another process already moved it aside (no-flock platforms)
    except OSError as rename_err:
        _LOGGER.warning("Could not move corrupt metadata sidecar aside: %s", rename_err)


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
