"""
Atomic writes and replace-race-coherent reads for dashboard-internal artefacts.

For cert / key / token / metadata-sidecar files, plus the
mode-preserving rewrite of user-editable YAML.

Why not the upstream helper here: ``write_file``'s ``private``
flag is binary (0o644 vs 0o600), and we need arbitrary modes for
caller-controlled-mode shapes (cert / key + future token stores)
and for keeping an operator-set mode across a rewrite.

Blocking I/O — call from ``run_in_executor``, not the loop.
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

# Windows raises ``PermissionError`` (WinError 5) on both sides of a
# replace race: from ``os.replace`` when a transient handle holds the
# destination open, and from ``open()`` when a lock-free reader opens the
# file while a replace is mid-flight (sharing violation). POSIX rename is
# atomic and never hits either, so the retry is Windows-only. Backoff grows
# exponentially (capped) so the common sub-second window still clears on the
# first short wait, while a slow scanner under loaded CI gets several seconds
# before we give up — far cheaper than losing a metadata / preferences write
# or failing a concurrent read.
_IS_WINDOWS = os.name == "nt"
# ``os.link`` failures that mean "this filesystem can't hard-link"
# (SMB / FAT / some FUSE mounts) rather than a real error.
_HARDLINK_UNSUPPORTED_ERRNOS = frozenset({errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS})
_REPLACE_RETRIES = 15
_REPLACE_RETRY_BACKOFF_S = 0.05
_REPLACE_RETRY_BACKOFF_CAP_S = 0.5


def atomic_write(
    path: Path, data: bytes, *, mode: int | None = None, make_parents: bool = False
) -> None:
    """
    Write *data* to *path* atomically.

    Stages bytes in a sibling tempfile, then ``Path.replace``s into
    place. Readers see either the old or new bytes, never a
    truncated file. ``mode`` is applied to the staging file before
    the rename; ``Path.replace`` carries that mode to the destination.
    ``make_parents`` creates ``path.parent`` (and any missing
    ancestors) first, for callers whose target directory may not
    exist yet.
    """
    if make_parents:
        path.parent.mkdir(parents=True, exist_ok=True)
    _staged_write(path, data, mode=mode, publish=_replace_with_retry)


def atomic_write_preserving_mode(path: Path, data: bytes, *, default_mode: int = 0o644) -> None:
    """
    Atomically write *data*, keeping an existing *path*'s permission bits.

    A rewrite must not reset an operator-tightened mode (e.g. a 0600
    ``secrets.yaml``) back to world-readable; a missing (or unstatable)
    *path* gets *default_mode*.
    """
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        mode = default_mode
    atomic_write(path, data, mode=mode)


def atomic_write_exclusive(path: Path, data: bytes, *, mode: int = 0o644) -> None:
    """
    Write *data* to *path* atomically; ``FileExistsError`` when it exists.

    Stages like :func:`atomic_write`, then publishes with an exclusive
    primitive — ``os.link`` on POSIX, ``os.rename`` on Windows — both
    of which refuse an existing target. The target only ever appears
    fully written (a crash mid-write leaves no partial file) and a
    concurrent creator loses cleanly with ``FileExistsError``. On a
    hardlink-less POSIX filesystem (SMB / FAT) the publish degrades to
    a direct exclusive write: still no-clobber, but a crash mid-write
    can leave a partial target on that one mount class.
    """
    _staged_write(path, data, mode=mode, publish=_publish_exclusive)


def read_bytes_with_retry(path: Path) -> bytes:
    """Read *path*'s bytes, retried on a Windows handle race with a concurrent replace."""
    return _retry_windows_permission(path.read_bytes)


def read_text_with_stat(path: Path) -> tuple[os.stat_result, str]:
    """Read *path* off one open handle, pairing the content with the handle's ``fstat``."""

    def _read() -> tuple[os.stat_result, str]:
        with path.open(encoding="utf-8") as fh:
            return os.fstat(fh.fileno()), fh.read()

    return _retry_windows_permission(_read)


def _staged_write(
    path: Path,
    data: bytes,
    *,
    mode: int | None,
    publish: Callable[[Path, Path], None],
) -> None:
    """
    Stage *data* in a sibling tempfile, then hand it to *publish*.

    *publish* moves or links the staged file onto *path*; the staged
    tempfile is cleaned up afterwards either way (a suppressed no-op
    when the publish consumed it). Readers of *path* only ever see a
    fully written file.
    """
    fd, tmp_str = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_str)
    try:
        # ``os.fdopen`` itself can raise (rare; ENOMEM, bad fd).
        # If it does, the with-block never enters, so fd would
        # leak; close explicitly here and re-raise.
        try:
            fh = os.fdopen(fd, "wb")
        except Exception:
            with contextlib.suppress(OSError):
                os.close(fd)
            raise
        with fh:
            if mode is not None:
                tmp_path.chmod(mode)
            fh.write(data)
        publish(tmp_path, path)
    finally:
        # Suppress all OSError on cleanup so a write failure isn't
        # masked by a secondary unlink permission error.
        with contextlib.suppress(OSError):
            tmp_path.unlink()


def _publish_exclusive(src: Path, dst: Path) -> None:
    """Exclusive publish: refuses an existing *dst* with ``FileExistsError``."""
    if not _IS_WINDOWS:
        # ``os.link`` is the POSIX exclusive primitive; the staged
        # source is unlinked by the caller's cleanup.
        try:
            os.link(src, dst)
        except FileExistsError:
            raise
        except OSError as err:
            if err.errno not in _HARDLINK_UNSUPPORTED_ERRNOS:
                raise
        else:
            return
        # Hardlink-less filesystem: keep the exclusive-open reach the
        # pre-staged write had, at the cost of unstaged bytes on this
        # one exotic mount class. Mirror the staged file's mode; a
        # fresh ``open("xb")`` would otherwise take the umask default.
        with dst.open("xb") as f:
            f.write(src.read_bytes())
        dst.chmod(stat.S_IMODE(src.stat().st_mode))
        return
    # ``Path.rename`` on Windows refuses an existing destination
    # (``FileExistsError``, surfaced immediately, not a
    # ``PermissionError``); a transient hold by an AV / indexer gets
    # the shared retry policy.
    _retry_windows_permission(lambda: src.rename(dst))


def _replace_with_retry(src: Path, dst: Path) -> None:
    """``Path.replace`` *src* onto *dst*, retried on a Windows handle race."""
    _retry_windows_permission(lambda: src.replace(dst))


def _retry_windows_permission[T](op: Callable[[], T]) -> T:
    """
    Run *op*, retrying a transient Windows ``PermissionError`` with backoff.

    POSIX never hits the transient class, so a ``PermissionError``
    there is real and surfaces without a retry.
    """
    for attempt in range(_REPLACE_RETRIES):
        try:
            return op()
        except PermissionError:
            if not _IS_WINDOWS or attempt == _REPLACE_RETRIES - 1:
                raise
            time.sleep(min(_REPLACE_RETRY_BACKOFF_S * 2**attempt, _REPLACE_RETRY_BACKOFF_CAP_S))
    raise AssertionError("unreachable: loop returns or raises")  # pragma: no cover
