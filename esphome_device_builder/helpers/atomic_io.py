"""
Atomic filesystem writes for dashboard-internal artefacts.

For cert / key / token / metadata-sidecar files. User-editable
YAML still goes through :func:`esphome.helpers.write_file`.

Why not the upstream helper here: ``write_file``'s ``private``
flag is binary (0o644 vs 0o600), and we need arbitrary modes for
caller-controlled-mode shapes (cert / key + future token stores).

Blocking I/O — call from ``run_in_executor``, not the loop.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import time
from pathlib import Path

# Windows raises ``PermissionError`` (WinError 5) from ``os.replace`` when a
# transient handle holds the destination open (a concurrent reader, an
# antivirus or the search indexer). POSIX rename is atomic and never hits
# this, so the retry is Windows-only. Backoff grows exponentially (capped)
# so the common sub-second window still clears on the first short wait,
# while a slow scanner under loaded CI gets several seconds before we give
# up — far cheaper than losing a metadata / preferences write.
_IS_WINDOWS = os.name == "nt"
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
        _replace_with_retry(tmp_path, path)
    except Exception:
        # Suppress all OSError on cleanup so the original write
        # failure isn't masked by a secondary unlink permission
        # error.
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def _replace_with_retry(src: Path, dst: Path) -> None:
    """``Path.replace`` *src* onto *dst*, retried on a Windows handle race."""
    for attempt in range(_REPLACE_RETRIES):
        try:
            src.replace(dst)
        except PermissionError:
            # POSIX rename can't hit this transiently, so a PermissionError
            # there is real; re-raise. On Windows, back off and retry.
            if not _IS_WINDOWS or attempt == _REPLACE_RETRIES - 1:
                raise
            time.sleep(min(_REPLACE_RETRY_BACKOFF_S * 2**attempt, _REPLACE_RETRY_BACKOFF_CAP_S))
        else:
            return
