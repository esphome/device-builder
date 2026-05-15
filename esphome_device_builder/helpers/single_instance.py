"""
Single-process-per-``data_dir`` startup lock.

Keyed on ``CORE.data_dir`` rather than ``config_dir`` so the HA
addon's Prod/Beta/DEV flavors — distinct ``/data`` mounts but a
shared ``/config/esphome`` — can run in parallel; the
race-prone state (build tree, firmware queue, StorageJSON) all
lives in ``data_dir`` and is per-flavor there. Same-data-dir
double-launches (dev mode against the same ``--config-dir``)
are still caught. Cross-flavor writes to the shared
``.device-builder.json`` are serialised separately by an
``fcntl.flock`` inside :func:`controllers.config.metadata_transaction`.

Mirrors :func:`homeassistant.runner.ensure_single_execution`:
open ``<data_dir>/.device-builder.lock`` in append mode, take
an exclusive non-blocking ``fcntl.flock``, and on success write
a ``{pid, lock_format_version, device_builder_version, start_ts}``
JSON record for diagnostics. On contention, surface the running
PID + start time on stderr and return ``exit_code=1`` for the
caller to honour.

The lock is held for the dashboard's lifetime — the OS releases
the ``flock`` automatically when the process exits (clean
shutdown or crash), so no cleanup logic is required and a stale
``.device-builder.lock`` file (no live holder) is harmless: the
next start re-acquires the flock cleanly.

POSIX-only. Windows lacks ``fcntl``; the context manager
degrades to a silent no-op there. The HA-addon shape is the
dominant production target and is POSIX-only; dev / Desktop on
Windows accept the residual race risk in exchange for not
needing ``msvcrt.locking`` plumbing.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import stat
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from io import TextIOWrapper
from pathlib import Path

try:
    import fcntl

    _HAS_FCNTL = True
except ImportError:  # pragma: no cover — Windows path
    _HAS_FCNTL = False

from ..constants import __version__

_LOGGER = logging.getLogger(__name__)

_LOCK_FILE_NAME = ".device-builder.lock"
# Increment when the JSON payload shape changes — old dashboards
# reading a future format will at least print "Unable to read
# lock file details" rather than crash on a missing key.
_LOCK_FORMAT_VERSION = 1


@dataclasses.dataclass(slots=True)
class SingleInstanceLock:
    """
    Status object yielded by :func:`ensure_single_execution`.

    ``exit_code`` is ``None`` on success (caller should run
    normally) and ``1`` on contention (caller should
    ``sys.exit(lock.exit_code)`` after the context exits — the
    diagnostic message has already been printed to stderr).
    """

    exit_code: int | None = None


def _write_lock_info(lock_file: TextIOWrapper) -> None:
    """Write our PID + start info into *lock_file* (already at offset 0)."""
    lock_file.seek(0)
    lock_file.truncate()
    json.dump(
        {
            "pid": os.getpid(),
            "lock_format_version": _LOCK_FORMAT_VERSION,
            "device_builder_version": __version__,
            "start_ts": time.time(),
        },
        lock_file,
    )
    lock_file.flush()


def _report_existing_instance(lock_file_path: Path, lock_dir: Path) -> None:
    """
    Print diagnostics about the running instance to stderr.

    Best-effort: an empty / unreadable / partially-written lock
    file falls back to the "Unable to read lock file details"
    line so we always print *something* useful (the data dir
    path + the "stop the existing instance" guidance) rather than
    swallowing the contention silently.
    """
    error_lines: list[str] = ["Error: Another device-builder is already running!"]
    # The exception list is broad on purpose — this is a
    # best-effort diagnostic helper, and crashing it (e.g. a
    # ``UnicodeDecodeError`` from a non-UTF8 lock file, or a
    # ``TypeError`` from a future schema where ``start_ts`` is
    # a string instead of a float) would defeat the whole point
    # of surfacing the contention to the operator. ``OSError``
    # covers read failures (permissions, ENOENT race);
    # ``UnicodeDecodeError`` covers a corrupted / non-UTF8 body;
    # ``json.JSONDecodeError`` covers a partial flush;
    # ``KeyError`` / ``TypeError`` / ``ValueError`` /
    # ``OverflowError`` cover schema drift between dashboard
    # versions (missing or wrong-type fields,
    # ``datetime.fromtimestamp`` rejecting an out-of-range
    # value).
    try:
        content = lock_file_path.read_text(encoding="utf-8").strip()
        if content:
            existing = json.loads(content)
            # ``start_ts`` is a Unix timestamp (UTC by definition);
            # convert to a tz-aware local datetime so the operator
            # sees the start time in their wall clock with the
            # platform's locale abbreviation below.
            start_dt = datetime.fromtimestamp(existing["start_ts"], tz=UTC).astimezone()
            # Locale's tz abbrev when the platform supports it
            # (most Unixen do); falls back to the unannotated
            # local-time stamp on bare-metal platforms whose
            # ``%Z`` returns empty.
            if tz_abbr := start_dt.strftime("%Z"):
                start_time = start_dt.strftime(f"%Y-%m-%d %H:%M:%S {tz_abbr}")
            else:
                start_time = start_dt.strftime("%Y-%m-%d %H:%M:%S") + " (local time)"
            error_lines.append(f"  PID: {existing['pid']}")
            error_lines.append(f"  Version: {existing.get('device_builder_version', '<unknown>')}")
            error_lines.append(f"  Started: {start_time}")
        else:
            error_lines.append("  Unable to read lock file details.")
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
        OverflowError,
    ) as exc:
        error_lines.append(f"  Unable to read lock file details: {exc}")
    error_lines.append(f"  Data directory: {lock_dir}")
    error_lines.append("")
    error_lines.append("Stop the existing instance before starting a second one.")
    for line in error_lines:
        print(line, file=sys.stderr)  # noqa: T201


def _open_lock_file(path: str, flags: int) -> int:
    """
    ``open(...)`` opener that adds ``O_NOFOLLOW`` to reject symlinks.

    The ``a+`` mode passes ``O_RDWR | O_APPEND | O_CREAT`` here;
    we OR in ``O_NOFOLLOW`` so a symlink at the lock-file path
    raises ``ELOOP`` instead of being silently followed and
    truncated by the downstream ``_write_lock_info``. Mode 0o644
    matches the umask-default we'd get from a plain ``open()``
    so the lock file is operator-readable for ``cat`` / ``ps``
    cross-referencing.
    """
    return os.open(path, flags | os.O_NOFOLLOW, 0o644)


@contextmanager
def ensure_single_execution(lock_dir: Path) -> Generator[SingleInstanceLock]:
    """
    Acquire the per-``lock_dir`` startup lock; yield a status object.

    Callers pass ``CORE.data_dir``. The flock is held for the
    lifetime of the ``with`` block. On contention, sets
    ``lock.exit_code = 1`` and prints diagnostics; the caller
    should ``sys.exit(lock.exit_code)`` after the context exits.
    Windows / no-fcntl: silent no-op.
    """
    lock = SingleInstanceLock()

    if not _HAS_FCNTL:
        _LOGGER.debug("fcntl unavailable; single-instance lock skipped")
        yield lock
        return

    lock_file_path = Path(lock_dir) / _LOCK_FILE_NAME

    # ``CORE.data_dir`` doesn't exist yet on a fresh default-mode
    # install — created lazily by the first compile, which runs
    # after the lock. ``O_CREAT`` makes the file, not its parent.
    try:
        lock_file_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        _LOGGER.exception(
            "Could not create lock directory %s (refusing to start)",
            lock_file_path.parent,
        )
        lock.exit_code = 1
        yield lock
        return

    # ``a+`` so the previous instance's diagnostic record stays
    # readable until our flock acquisition succeeds — the
    # contention path reads it back to print the running PID.
    # Truncating before the lock is held would drop the very
    # information the operator needs.
    #
    # Custom ``opener=`` adds ``O_NOFOLLOW`` so a symlink at the
    # lock-file path is rejected with ``ELOOP`` instead of being
    # followed and truncated. Without this, an attacker (or a
    # misconfigured environment) with write access to
    # ``<data_dir>`` could place a symlink at
    # ``.device-builder.lock -> /etc/passwd`` (or any other path
    # the dashboard process can write) and have ``_write_lock_info``
    # truncate the link target on every start. The ``fstat``
    # regular-file check downstream catches the more obscure
    # case where the path is something else exotic — a FIFO,
    # device node, etc. — that ``O_NOFOLLOW`` doesn't bar but
    # ``open()`` happily follows.
    try:
        lock_file_ctx = open(  # noqa: SIM115 — closed in finally
            lock_file_path, "a+", encoding="utf-8", opener=_open_lock_file
        )
    except OSError:
        _LOGGER.exception(
            "Could not open lock file %s (refusing to start)",
            lock_file_path,
        )
        lock.exit_code = 1
        yield lock
        return
    with lock_file_ctx as lock_file:
        st = os.fstat(lock_file.fileno())
        if not stat.S_ISREG(st.st_mode):
            _LOGGER.error(
                "Lock file %s is not a regular file (mode=%o); refusing to start",
                lock_file_path,
                st.st_mode,
            )
            lock.exit_code = 1
            yield lock
            return
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _report_existing_instance(lock_file_path, Path(lock_dir))
            lock.exit_code = 1
            yield lock
            return
        _write_lock_info(lock_file)
        # The ``with open`` exit closes the fd, which the OS
        # uses as the trigger to release the flock. No explicit
        # ``LOCK_UN`` here — we'd just be racing the close.
        yield lock
