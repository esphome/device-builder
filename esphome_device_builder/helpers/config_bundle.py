"""
Build a self-contained ESPHome bundle from a YAML on disk.

Wraps the ``esphome bundle <yaml> -o <tarball>`` CLI for the
offloader-side ``submit_job`` flow: the remote runner hands a
YAML path, this module returns the gzipped-tar bytes ready
for chunking onto the peer-link, streaming the subprocess
output to an optional per-line callback.

Subprocess rather than in-process
:class:`esphome.bundle.ConfigBundleCreator` so the bundle
build:

* Doesn't depend on ESPHome's in-process API surface (which
  evolves between releases — calling :func:`esphome.config.read_config`
  + ``ConfigBundleCreator`` directly couples us to the
  ``CORE`` global and the validation pipeline). The CLI's
  ``bundle`` subcommand is part of the user-facing contract
  and shifts much less.
* Doesn't race the dashboard's own ``CORE.config_path`` —
  every other controller path leans on ``CORE.config_path``
  being set to the dashboard sentinel; mutating it in-process
  would force a lock + save / restore dance and still leave
  a window where a concurrent ``ext_storage_path`` reader
  sees the wrong layout.
* Matches the existing pattern the firmware controller uses
  for every compile / upload (subprocess, same
  :func:`_find_esphome_cmd` resolver), so a future ESPHome
  bump only has to be validated against one integration
  surface.

Errors:

* :class:`FileNotFoundError` from missing YAML — propagated;
  the caller fails the job as configuration-not-found.
* :class:`BundleBuildError` — esphome bundle exited non-zero
  (typically schema-invalid YAML, missing include, malformed
  secret) or timed out; :attr:`BundleBuildError.output`
  carries the captured stdout/stderr unless streamed.
* :class:`OSError` from the spawn itself (e.g. ``esphome``
  not on PATH) propagates.
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Callable
from pathlib import Path

from .async_ import run_in_executor
from .sibling_cli import _find_esphome_cmd
from .subprocess import run_subprocess_capture

_LOGGER = logging.getLogger(__name__)

# Bound for the ``esphome bundle`` subprocess. Bundle build
# is dominated by ``read_config`` (parses every include,
# resolves every component schema, may fetch remote packages)
# + tar packing; a typical ~50-file config completes in <2s,
# but heavy include trees with slow validation legitimately
# run for minutes (the 60s original stranded a real user's
# remote builds). The output streams into the job log, so a
# long wait is visible; the bound only stops a truly wedged
# subprocess from pinning the job forever.
_BUNDLE_BUILD_TIMEOUT_SECONDS = 300.0


class BundleBuildError(RuntimeError):
    """``esphome bundle`` subprocess failed or timed out.

    :attr:`output` carries the captured stdout/stderr for a
    non-streaming caller; with *on_output* the callback owns
    retention and :attr:`output` is empty.
    """

    def __init__(self, message: str, *, output: str) -> None:
        super().__init__(message)
        self.output = output


async def build_yaml_bundle(
    yaml_path: Path, *, on_output: Callable[[str], None] | None = None
) -> bytes:
    """Build a gzipped-tar bundle for *yaml_path* and return its raw bytes.

    Spawns ``esphome bundle <yaml_path> -o <tmp.tar.gz>``,
    streams each output chunk to *on_output* as it arrives,
    awaits completion, reads the resulting bytes back, and
    deletes the temp file. The temp file lives in the
    platform's default tmp dir (typically ``/tmp/`` or
    ``$TMPDIR``); the dashboard never writes user-visible
    files outside ``config_dir``.

    Cancellation-safe: the subprocess is killed and the temp
    file is unlinked in a ``finally`` regardless of how the
    function exits (including ``CancelledError`` from the
    caller being cancelled mid-build).
    """
    # Every filesystem syscall here (``is_file`` → ``os.stat``,
    # ``_find_esphome_cmd`` → ``Path.exists`` → ``os.stat``,
    # ``NamedTemporaryFile`` → ``os.open``, ``read_bytes`` →
    # ``os.read``, ``unlink`` → ``os.unlink``) is blocking;
    # blockbuster catches them when run on the event loop in CI.
    # Batch the upfront syscalls into one executor hop, then
    # stage the post-subprocess read + unlink through their own
    # hops so the dashboard's other tasks keep moving on slow
    # disks.
    cmd, output_path = await run_in_executor(_prepare_build_bundle, yaml_path)
    try:
        result = await run_subprocess_capture(
            *cmd,
            "bundle",
            str(yaml_path),
            "-o",
            str(output_path),
            timeout=_BUNDLE_BUILD_TIMEOUT_SECONDS,
            on_line=on_output,
        )
        output = result.stdout.decode("utf-8", errors="replace").strip()
        if result.timed_out:
            raise BundleBuildError(
                f"esphome bundle timed out after {_BUNDLE_BUILD_TIMEOUT_SECONDS:.0f}s",
                output=output,
            )
        if result.returncode != 0:
            raise BundleBuildError(f"esphome bundle exited {result.returncode}", output=output)
        return await run_in_executor(output_path.read_bytes)
    finally:
        await run_in_executor(_unlink_quietly, output_path)


def _prepare_build_bundle(yaml_path: Path) -> tuple[list[str], Path]:
    """Sync prep step: validate YAML exists, resolve esphome cmd, reserve temp path.

    Bundles every upfront blocking syscall into one executor
    hop. Raises :class:`FileNotFoundError` if *yaml_path*
    doesn't exist.
    """
    if not yaml_path.is_file():
        msg = f"YAML not found: {yaml_path}"
        raise FileNotFoundError(msg)
    return _find_esphome_cmd(), _allocate_temp_bundle_path()


def _allocate_temp_bundle_path() -> Path:
    """Reserve a unique ``.tar.gz`` path under the platform tmp dir.

    ``delete=False`` because we close the FD immediately — the
    subprocess writes to the path, and the caller unlinks in
    its ``finally``. :class:`tempfile.NamedTemporaryFile` is
    just the name-reservation primitive (TMPDIR-aware on every
    platform); no FD is held open across the subprocess.
    """
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        return Path(tmp.name)


def _unlink_quietly(path: Path) -> None:
    """Best-effort ``unlink`` that swallows ``OSError``."""
    try:
        path.unlink()
    except OSError:
        _LOGGER.debug("failed to unlink temp bundle %s", path, exc_info=True)
