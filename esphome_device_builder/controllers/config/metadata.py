"""Metadata sidecar (.device-builder.json) persistence — atomic RMW + device fields."""

from __future__ import annotations

import os
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:
    import fcntl

    _HAS_FCNTL = True
except ImportError:  # pragma: no cover — Windows path
    _HAS_FCNTL = False

from ...helpers.atomic_io import atomic_write
from ...helpers.json import JSONDecodeError, dumps_indent, loads

_METADATA_FILE = ".device-builder.json"
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


def _open_metadata_lock_file(path: str, flags: int) -> int:
    """``open()`` opener that adds ``O_NOFOLLOW`` to reject symlinks."""
    return os.open(path, flags | os.O_NOFOLLOW, 0o644)


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
            data = _load_metadata(config_dir)
            yield data
            _save_metadata(config_dir, data)
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
            data = _load_metadata(config_dir)
            yield data
            _save_metadata(config_dir, data)


def _load_metadata(config_dir: Path) -> dict[str, Any]:
    path = config_dir / _METADATA_FILE
    try:
        # orjson decodes bytes directly, so skip the read_text → encode
        # round-trip. JSONDecodeError is a subclass of ValueError.
        data = loads(path.read_bytes())
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, JSONDecodeError):
        return {}


def _save_metadata(config_dir: Path, data: dict[str, Any]) -> None:
    # Atomic so lock-free readers never observe a partial write.
    # ``dumps_indent`` yields bytes; the on-disk file stays readable / diffable.
    atomic_write(config_dir / _METADATA_FILE, dumps_indent(data))


def get_board_id(config_dir: Path, filename: str) -> str:
    """Get the board_id for a device."""
    return str(_load_metadata(config_dir).get(filename, {}).get("board_id", ""))


def set_device_metadata(
    config_dir: Path,
    filename: str,
    *,
    board_id: str | None = None,
    friendly_name: str | None = None,
    comment: str | None = None,
    ip: str | None = None,
    expected_config_hash: str | None = None,
    mac_address: str | None = None,
    regen_failed_mtime: float | None = None,
    regen_failed_at: float | None = None,
    build_size_bytes: int | None = None,
    build_size_dir_mtime: int | None = None,
    build_size_info_mtime: int | None = None,
    labels: list[str] | None = None,
) -> None:
    """
    Set metadata fields for a device.

    ``ip`` is the last-known resolved IP — persisted so the address
    cache survives backend restarts. Pass an empty string to leave the
    persisted value unchanged (mDNS clears the in-memory IP whenever a
    device drops off the network, but the cache is still useful).

    ``expected_config_hash`` is the 8-char hex FNV-1a-32 hash of the
    YAML as last successfully compiled — pair it with the mDNS
    ``config_hash`` TXT record (esphome/esphome#16145) to tell whether
    the running firmware matches the compiled config. Passing an empty
    string clears it (e.g. after a YAML edit invalidates the prior
    compile).

    ``mac_address`` is the canonical ``XX:XX:XX:XX:XX:XX`` MAC
    from the mDNS ``mac`` TXT record (normalized at ingest).
    Persisted so the dashboard renders the address immediately on
    startup, before the first mDNS probe response. Passing an
    empty string clears it.

    ``regen_failed_mtime`` is the YAML's mtime when the last
    ``--only-generate`` storage-regen attempt failed; pair it with
    ``regen_failed_at`` (the wall-clock time the failure was
    recorded). Together they let a backend restart skip retrying
    the same broken config (missing ``!secret`` / ``!include`` /
    unreachable git package) — the next attempt only runs when
    the YAML's mtime has actually moved past the cached stamp,
    OR when the cached stamp is older than the controller's
    failure-TTL (so transient external problems eventually get
    re-checked). The two fields are written together by
    :meth:`DevicesController._stamp_regen_failure`; the
    success / archive paths clear them by passing ``0.0`` to
    *both* — clearing only one half leaves the other behind, so
    callers should always touch the pair as a unit.

    ``build_size_bytes`` caches the total size of the per-device
    ``.esphome/build/<name>/`` tree at the freshness pair
    captured by the last walk. The pair is split because each
    half catches a class of compile-time changes the other
    misses: ``build_size_dir_mtime`` moves on entry-set churn
    (PlatformIO atomic-replaces, sibling add/remove),
    ``build_size_info_mtime`` moves on every real ESPHome
    recompile (``write_file_if_changed`` rewrites
    ``build_info.json``). Either side moving counts as stale,
    so a freshly-restarted dashboard re-walks any device whose
    pair drifted from what was persisted. Pass ``0`` for any
    field to clear (used by the archive flow's volatile-field
    scrub).

    ``labels`` is the list of label IDs assigned to this device
    (opaque ``uuid.uuid4().hex`` references into the global
    ``_labels`` catalog). ``None`` leaves the persisted list
    alone; ``[]`` clears it (drops the key entirely so empty
    entries don't bloat the file); a populated list replaces
    the assignments wholesale.
    """
    with metadata_transaction(config_dir) as data:
        entry = data.setdefault(filename, {})
        if board_id is not None:
            entry["board_id"] = board_id
        if friendly_name is not None:
            entry["friendly_name"] = friendly_name
        if comment is not None:
            entry["comment"] = comment
        if ip:
            entry["ip"] = ip
        if labels is not None:
            if labels:
                entry["labels"] = list(labels)
            else:
                entry.pop("labels", None)
        # Tri-state fields: ``None`` means "leave alone", a truthy
        # value writes, an explicit falsy (``""`` / ``0``) clears.
        # The numeric stamps below (``regen_failed_*`` /
        # ``build_size_*``) all carry timestamps or sizes whose
        # legitimate values are strictly positive — ``0`` is
        # therefore safe as the explicit-clear sentinel.
        # Loop over the (key, value) pairs so adding a new
        # tri-state field doesn't bump this function's branch
        # count (ruff PLR0912 caps at 12).
        for key, value in (
            ("expected_config_hash", expected_config_hash),
            ("mac_address", mac_address),
            ("regen_failed_mtime", regen_failed_mtime),
            ("regen_failed_at", regen_failed_at),
            ("build_size_bytes", build_size_bytes),
            ("build_size_dir_mtime", build_size_dir_mtime),
            ("build_size_info_mtime", build_size_info_mtime),
        ):
            if value is None:
                continue
            if value:
                entry[key] = value
            else:
                entry.pop(key, None)


def get_device_metadata(config_dir: Path, filename: str) -> dict[str, Any]:
    """Get all metadata for a device."""
    result = _load_metadata(config_dir).get(filename, {})
    return result if isinstance(result, dict) else {}


def get_device_ip(config_dir: Path, filename: str) -> str:
    """Return the last-known resolved IP for a device, or ``""`` if unknown."""
    return str(_load_metadata(config_dir).get(filename, {}).get("ip", ""))


def remove_device_metadata(config_dir: Path, filename: str) -> None:
    """Remove metadata for a device."""
    with metadata_transaction(config_dir) as data:
        data.pop(filename, None)


# Per-device shared-sidecar fields that go stale on archive.
# After the per-device live state moved into the data-dir store,
# only ``mac_address`` remains in the shared sidecar with archive-
# volatile semantics: it's intrinsic to the physical board, but
# unarchive may rebind the YAML to a different board, so the
# cached MAC must clear. Everything else here is identity that
# survives archive.
_VOLATILE_DEVICE_METADATA_FIELDS: frozenset[str] = frozenset({"mac_address"})


def clear_volatile_device_metadata(config_dir: Path, filename: str) -> None:
    """Drop runtime / observed state fields, keep stable identity fields.

    On archive the dashboard removes the YAML's compile output
    and the StorageJSON sidecar (both are build artifacts), but
    the device-metadata entry carries a mix of:

    - Stable identity fields (``board_id``, ``friendly_name``,
      ``comment``) — set by the user or derived from the YAML
      itself, still meaningful on unarchive.
    - Volatile fields (``ip``, ``expected_config_hash``) —
      describe the firmware / network state at archive time and
      go stale immediately.

    The earlier shape removed the entire entry on archive, which
    closed the "future same-name device inherits stale state"
    risk but also lost the identity fields. The catalog → YAML
    match key is ``board_id``; losing it on every archive →
    unarchive cycle forced a re-derive (or a re-pick by the
    user) that wasn't necessary. This helper preserves identity
    + clears volatile so unarchive restores the user-visible
    state unchanged. Same-name new-device leakage of identity
    fields is acceptable: the new device's create flow either
    derives or supplies its own ``board_id``, and friendly_name
    / comment are user labels the new device's editor can
    overwrite if desired.
    """
    with metadata_transaction(config_dir) as data:
        entry = data.get(filename)
        if entry is None:
            return
        if not isinstance(entry, dict):
            # Treat a non-dict value as corrupt — leaving it in place
            # would later break ``set_device_metadata`` (which assumes
            # the existing entry is a dict and item-assigns into it).
            # Drop the bad value so the next write starts from a
            # clean shape.
            data.pop(filename, None)
            return
        for field_name in _VOLATILE_DEVICE_METADATA_FIELDS:
            entry.pop(field_name, None)
        # If the entry is now empty (no identity fields ever
        # set) drop it entirely so we don't leave dead keys
        # behind in the metadata file.
        if not entry:
            data.pop(filename, None)
