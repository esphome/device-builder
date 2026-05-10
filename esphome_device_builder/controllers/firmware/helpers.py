"""
Pure helpers for the firmware controller.

Free functions only — no controller state. ``_find_esphome_cmd`` is
imported directly by ``editor.py`` and ``devices.py``; the rest are
used inside ``controller.py`` and exercised in isolation by tests
under ``tests/controllers/firmware/test_helpers.py``.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from ...helpers.api import CommandError
from ...helpers.subprocess import run_subprocess_capture
from ...models import (
    TERMINAL_JOB_STATUSES,
    ErrorCode,
    FirmwareJob,
    JobStatus,
    JobType,
)
from .constants import (
    _MAX_OUTPUT_LINES_RETAINED,
    _NO_ESPHOME_MODULE_MARKER,
    _OUTPUT_TRIM_NOTICE_PREFIX,
    _PROGRESS_PATTERNS,
)

_LOGGER = logging.getLogger(__name__)


def _is_no_module_named_esphome(text: str) -> bool:
    """Return True if *text* names ``esphome`` itself as missing.

    Module-level helper so the at-append capture in the runner and
    its regression test both call the same function — without this
    the test reimplemented the substring check locally and could
    silently pass against a regressed production closure.
    """
    return _NO_ESPHOME_MODULE_MARKER in text


def _trim_job_output(job: FirmwareJob, *, keep: int = _MAX_OUTPUT_LINES_RETAINED) -> None:
    """
    Cap ``job.output`` at the last ``keep`` lines.

    Mutates the job in place. Safe to call repeatedly on the same
    job — already-trimmed output stays stable and the elided count
    keeps growing as new lines are dropped.

    ``keep`` is the same value (``_MAX_OUTPUT_LINES_RETAINED``) for
    both the in-flight and post-completion call sites. The two
    paths differ only in their *trigger*: the in-flight path
    invokes this from the streaming loop when ``len(job.output)``
    crosses ``_MAX_OUTPUT_LINES_INFLIGHT`` (=``2 * keep``), so
    every trim drops back to ``keep`` and leaves a ``keep``-line
    headroom before the next trim fires. The post-completion call
    uses the default keep, so a build that finished under the
    in-flight cap is trimmed once on exit; a build that already
    triggered the in-flight trim is at ``keep`` lines plus the
    elided notice and this final call is a no-op for it.
    """
    output = job.output
    extra_elided = 0
    # Recover and fold in the previous elided count so repeated trims
    # don't pretend only one line was dropped on each subsequent call.
    if output and output[0].startswith(_OUTPUT_TRIM_NOTICE_PREFIX):
        match = re.search(r"(\d+) earlier", output[0])
        if match:
            extra_elided = int(match.group(1))
        output = output[1:]
    if len(output) <= keep:
        return
    new_elided = len(output) - keep
    total_elided = extra_elided + new_elided
    job.output = [
        f"{_OUTPUT_TRIM_NOTICE_PREFIX} {total_elided} earlier line(s) elided]\n",
        *output[-keep:],
    ]


def _mark_job_terminal(job: FirmwareJob, status: JobStatus) -> None:
    """
    Set *job* to a terminal *status* and stamp its completion time.

    The two writes go together at every job-finalisation site
    (queued cancel, mid-run cancel, normal completion, runner-shutdown
    cancel, exception, reset-build-env cancel/complete), and forgetting
    one or the other is a recurring footgun — a status without a
    ``completed_at`` confuses the dashboard's relative-time tooltip,
    and a ``completed_at`` without a status leaves the job stuck on
    ``RUNNING`` even though the subprocess is gone.

    Pulling them into one call keeps the call sites readable and the
    pair atomic. Doesn't fire the lifecycle event — the call site
    decides which event to fire and in what order relative to
    ``_persist_jobs`` / ``_prune_history`` so the existing observable
    sequencing is preserved.

    Raises ``ValueError`` for any non-terminal *status* so a
    stray call (e.g. ``_mark_job_terminal(job, JobStatus.RUNNING)``)
    fails loudly instead of silently stamping ``completed_at`` on a
    still-running job — that would mis-order the dashboard's
    relative-time strings and confuse the prune-on-shutdown logic.
    """
    if status not in TERMINAL_JOB_STATUSES:
        msg = f"_mark_job_terminal called with non-terminal status {status!r}"
        raise ValueError(msg)
    job.status = status
    job.completed_at = datetime.now(UTC).isoformat()


def _names_touched_by_job(job: FirmwareJob) -> set[str]:
    """YAML filenames a job will read or write.

    Used by the rename-lock check to spot collisions between an
    in-flight rename and any other job. A rename has two: the old
    YAML it's reading from (``configuration``) and the new YAML it
    will create on install success (``new_name + ".yaml"``). Every
    other job type touches just one — its ``configuration``.
    """
    names: set[str] = set()
    if job.configuration:
        names.add(job.configuration)
    if job.job_type == JobType.RENAME and job.new_name:
        names.add(f"{job.new_name}.yaml")
    return names


def _find_esphome_cmd() -> list[str]:
    """Locate the ``esphome`` CLI, preferring the same interpreter as ours.

    The backend's own interpreter (``sys.executable``) is the
    authoritative source: if it can import ``esphome`` to start the
    server, it can run ``python -m esphome`` for compile jobs. We
    don't try to substitute a sibling ``python`` next to
    ``sys.executable`` — that's an easy way to silently jump to a
    different interpreter (e.g. a system Python without esphome
    installed) and produce confusing "No module named esphome"
    errors at compile time.

    A standalone ``esphome`` script in the *same* bin directory as
    our interpreter is preferred when present (slightly cheaper than
    ``python -m esphome`` and surfaces a friendlier traceback when
    something goes wrong inside esphome).
    """
    python = sys.executable
    bin_dir = Path(python).parent

    sibling_esphome = bin_dir / ("esphome.exe" if os.name == "nt" else "esphome")
    if sibling_esphome.exists():
        return [str(sibling_esphome)]

    return [python, "-m", "esphome"]


def _parse_progress(line: str) -> int | None:
    """Extract a 0-100 progress percentage from a build/flash output line.

    Returns ``None`` when the line doesn't match one of the known
    progress shapes (see ``_PROGRESS_PATTERNS``). Stray ``%`` signs
    elsewhere in the build output (Unpacking bars, memory-usage
    reports) are intentionally ignored.
    """
    for pattern in _PROGRESS_PATTERNS:
        match = pattern.search(line)
        if match is None:
            continue
        value = int(match.group(1))
        if 0 <= value <= 100:
            return value
    return None


def _validate_port(port: str) -> None:
    """Sanity-check the user-supplied ``--device`` value.

    The esphome CLI accepts arbitrary strings for ``--device`` and
    treats them as one of: the literal ``"OTA"`` (let the CLI
    resolve the configured host), a serial path, or a network host
    (IPv4 / IPv6 / ``.local`` hostname). Without an upfront check
    a typo'd IP would queue, run a compile, and only fail at the
    flash step with a CLI error buried in the job output. Validate
    early so the WS layer can return a clean ``INVALID_ARGS``.

    The check is deliberately permissive — any of these shapes is
    accepted:

    * Empty string (``upload`` default — CLI auto-detects)
    * The literal ``"OTA"``
    * A serial path: starts with ``/``, ``COM`` (Windows), or
      contains ``ttyUSB`` / ``ttyACM`` / ``cu.``
    * A valid IPv4 or IPv6 address
    * A hostname (``[a-z0-9-]+`` per label, optional ``.local``
      suffix, optional FQDN trailing dot) — covers
      ``device-name.local``, ``device.example.com.``, and bare
      hostnames

    Anything else (random punctuation, IPv4 with extra dots, etc.)
    raises ``CommandError(INVALID_ARGS)``. Coordinated frontend
    forms can pre-filter to the same shape.

    Error messages use neutral "device target" wording — this
    helper is shared across ``firmware/upload``, ``firmware/install``,
    and ``firmware/install_bulk``, and the message is surfaced
    verbatim over WS, so naming a single command in the error
    would mislead callers of the others.
    """
    if not port or port == "OTA":
        return
    # Serial paths.
    if (
        port.startswith("/")
        or port.startswith("COM")
        or any(marker in port for marker in ("ttyUSB", "ttyACM", "cu.", "tty."))
    ):
        return
    # IP-shaped input must parse as a valid IP. Doing this check
    # *before* the hostname check rejects truncated / malformed
    # IPv4 strings (``192.168.1``, ``256.256.256.256``) that would
    # otherwise pass the permissive hostname rules — RFC 1123
    # technically allows numeric hostnames, but a user typing
    # ``192.168.1`` meant an IP and we should fail loudly rather
    # than route it as ``--device 192.168.1`` to the CLI's DNS path.
    looks_ip = ":" in port or (port.replace(".", "").isdigit() and "." in port)
    if looks_ip:
        try:
            ipaddress.ip_address(port)
            return
        except ValueError as exc:
            raise CommandError(
                ErrorCode.INVALID_ARGS,
                f"Invalid device target {port!r} — looks like an IP but didn't parse: {exc}",
            ) from exc
    # Hostnames: a sequence of dot-separated labels, each
    # ``[a-z0-9](?:[a-z0-9-]*[a-z0-9])?``. Strip a single trailing
    # FQDN dot before matching — zeroconf and the system resolver
    # both produce trailing-dot forms (``kitchen.local.``,
    # ``device.example.com.``), and rejecting those would force
    # users to manually clean up addresses pasted from the mDNS
    # browser.
    canonical = port.removesuffix(".")
    if re.fullmatch(
        r"(?i)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*",
        canonical,
    ):
        return
    raise CommandError(
        ErrorCode.INVALID_ARGS,
        f"Invalid device target {port!r} — expected ``OTA``, a serial path, "
        f"an IP address, or a hostname",
    )


async def _verify_esphome_importable(cmd: list[str]) -> tuple[bool, str]:
    """Sanity-check that ``cmd`` can actually import esphome.

    Runs ``cmd --dashboard --version`` with a short timeout. Used at
    backend startup so misconfigured environments (venv missing
    esphome, wrong sys.executable, broken shim script) surface as a
    clear log line rather than a cryptic "No module named esphome"
    output captured during the user's first compile attempt.

    ``--dashboard`` is included in the probe so we also fail fast on
    an installed ESPHome that doesn't recognise the flag (very old
    builds): every real job command now passes ``--dashboard``, so a
    sanity check without it would let a broken pairing slip through to
    the user's first compile.

    Subprocess plumbing (timeout + kill_quietly + stdout decode)
    lives in :func:`helpers.subprocess.run_subprocess_capture`;
    shared with :func:`helpers.config_bundle.build_yaml_bundle`.
    """
    try:
        result = await run_subprocess_capture(*cmd, "--dashboard", "--version", timeout=15)
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if result.timed_out:
        return False, "TimeoutExpired: 15s probe didn't return"
    output = result.stdout.decode("utf-8", errors="replace").strip()
    if result.returncode != 0 or "No module named" in output or "ModuleNotFoundError" in output:
        return False, output or f"exit {result.returncode}"
    return True, output
