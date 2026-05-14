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
from pathlib import Path


def atomic_write(path: Path, data: bytes, *, mode: int | None = None) -> None:
    """
    Write *data* to *path* atomically.

    Stages bytes in a sibling tempfile, then ``Path.replace``s into
    place. Readers see either the old or new bytes, never a
    truncated file. ``mode`` is applied to the staging file before
    the rename; ``Path.replace`` carries that mode to the destination.
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
        tmp_path.replace(path)
    except Exception:
        # Suppress all OSError on cleanup so the original write
        # failure isn't masked by a secondary unlink permission
        # error.
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise
