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
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from ...helpers.api import CommandError
from ...helpers.subprocess import run_subprocess_capture
from ...models import (
    ErrorCode,
    EventType,
    FirmwareJob,
    JobLifecycleData,
    JobOutputData,
    JobProgressData,
    JobType,
)
from .constants import (
    _INFLIGHT_TRIM_KEEP,
    _MAX_OUTPUT_LINES_INFLIGHT,
    _MAX_OUTPUT_LINES_RETAINED,
    _NO_ESPHOME_MODULE_MARKER,
    _OUTPUT_TRIM_NOTICE_PREFIX,
    _PROGRESS_PATTERNS,
)

try:
    from esphome.upload_targets import PortType, get_port_type
except ModuleNotFoundError as exc:
    if exc.name != "esphome.upload_targets":
        raise
    from ._upload_targets_fallback import PortType, get_port_type

if TYPE_CHECKING:
    from ...helpers.event_bus import EventBus

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
    return list(_find_sibling_cli("esphome"))


def _find_esptool_cmd() -> list[str]:
    """Locate the ``esptool`` CLI, preferring the same interpreter as ours.

    Same sibling-script-first lookup as :func:`_find_esphome_cmd`.
    The sibling script's shebang is pinned to our interpreter so it
    can't accidentally jump to a different Python — and it dodges
    the ``"No module named esptool"`` failure mode under VS Code's
    debugpy launch chain, where ``python -m esptool`` from inside
    a debug-wrapped process can fail module resolution in ways the
    parent process doesn't.
    """
    return list(_find_sibling_cli("esptool"))


@lru_cache(maxsize=8)
def _find_sibling_cli(name: str, module: str | None = None) -> tuple[str, ...]:
    """Sibling script next to ``sys.executable``, else ``python -m <module or name>``.

    *module* lets the ``-m`` fallback target an import path that differs from the
    console-script *name* (e.g. ``device-builder-helper`` ->
    ``esphome_device_builder.helper_cli``); it defaults to *name*.

    Result is cached so the ``sibling.exists()`` filesystem probe
    runs once per ``name`` — async callers (``_run_esptool``,
    ``verify_chip``) would otherwise trip ``blockbuster`` on every
    invocation, since ``Path.exists`` calls ``os.stat`` synchronously.

    Returns a tuple so the cached value can't be mutated by callers
    that copy it into their own argv list.
    """
    python = sys.executable
    sibling = Path(python).parent / (f"{name}.exe" if os.name == "nt" else name)
    if sibling.exists():
        return (str(sibling),)
    return (python, "-m", module or name)


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
    """Accept ``""`` / ``"OTA"`` / SERIAL / IPv4-6 / hostname; raise ``INVALID_ARGS`` otherwise."""
    if not port or port == "OTA":
        return
    if get_port_type(port) is PortType.SERIAL:
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
        except ValueError as exc:
            raise CommandError(
                ErrorCode.INVALID_ARGS,
                f"Invalid device target {port!r} — looks like an IP but didn't parse: {exc}",
            ) from exc
        else:
            return
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


def _fire_job_progress(job: FirmwareJob, bus: EventBus, progress: int) -> None:
    """
    Stamp ``job.progress`` and fan out :attr:`EventType.JOB_PROGRESS`.

    The "set the field, fire the event" pair is invariant across
    every callsite — the only thing that differs is whether the
    caller has already gated the new value against the previous
    one (the streaming ingest does; the compile → upload phase
    transition deliberately doesn't, since the whole point there
    is to drop the gauge back to zero). The helper carries no
    clamp of its own so the gating policy stays at the callsite
    where it's readable.
    """
    job.progress = progress
    payload: JobProgressData = {"job_id": job.job_id, "progress": progress}
    bus.fire(EventType.JOB_PROGRESS, payload)


def _fire_job_lifecycle(job: FirmwareJob, bus: EventBus, event_type: EventType) -> None:
    """Fire a ``JobLifecycleData`` event (QUEUED / STARTED / a terminal status) for *job*."""
    payload: JobLifecycleData = {"job": job}
    bus.fire(event_type, payload)


def _ingest_output_line(job: FirmwareJob, bus: EventBus, line: str) -> None:
    r"""
    Append *line* to ``job.output`` and fire local follower events.

    Shared bookkeeping for "one line of build output arrived" —
    consumed by both the local subprocess streaming loop in
    :meth:`FirmwareController._execute_job` and the remote-source
    listener in :mod:`controllers.firmware.remote_runner`.

    Steps:

    1. Buffer the line on ``job.output``. CR-terminated chunks
       overwrite the previous entry instead of accumulating, so
       a ninja-driven ESP-IDF build's ~1200 "[N/total] …\r"
       updates don't fill the retention tail with overwritten
       progress lines (#898).
    2. Trim down to ``_INFLIGHT_TRIM_KEEP`` if the in-flight
       cap is hit, so a chatty build doesn't grow ``output``
       without bound between terminal-event trims.
    3. Fan it out as ``JOB_OUTPUT`` so live followers see it.
    4. Parse a coarse 0-100 progress percentage; if it
       advances the previous value, update the job and fire
       ``JOB_PROGRESS`` via :func:`_fire_job_progress`.
       Monotonic-clamp behaviour matches the local subprocess
       path (esptool's "100%" followed by PlatformIO's "0%"
       would otherwise look like a regression to the
       progress-bar renderer). Explicit phase transitions
       (compile → upload) call the helper directly to bypass
       the clamp and reset the gauge.

    Does **not** handle error-pattern detection — that's a
    local-only concern (the remote path gets a structured
    ``failed`` status from the receiver instead of having to
    scrape stderr).
    """
    # Collapse CR-overwritten progress lines at storage time: a
    # CR-terminated last entry followed by a non-bare-``\n`` chunk
    # gets replaced rather than retained. ``JOB_OUTPUT`` still fires
    # per chunk (live followers unchanged); only ``job.output``
    # (historical replay) is collapsed.
    if job.output and job.output[-1].endswith("\r") and line != "\n":
        job.output[-1] = line
    else:
        job.output.append(line)
    if len(job.output) > _MAX_OUTPUT_LINES_INFLIGHT:
        _trim_job_output(job, keep=_INFLIGHT_TRIM_KEEP)
    out_payload: JobOutputData = {"job_id": job.job_id, "line": line}
    bus.fire(EventType.JOB_OUTPUT, out_payload)
    progress = _parse_progress(line)
    if progress is None or progress <= (job.progress or 0):
        return
    _fire_job_progress(job, bus, progress)
