"""
Static configuration for the firmware controller.

Error-pattern regexes used to flag failures even when the
subprocess exit code is 0, terminal job-state sets shared by the
runner and the persistence/prune paths, and the queue/cleanup
tunables. Pure data — no I/O, no controller state. Imported
unchanged into ``controller.py``; tests reach for individual
constants from this module directly.
"""

from __future__ import annotations

import re

from ...models import EventType, JobStatus, JobType

# Metadata key under which the firmware queue persists itself in
# ``.device-builder.json``.
_JOBS_KEY = "_firmware_jobs"

# Output patterns that indicate failure even when the subprocess exit
# code is 0 (Python tracebacks routed through ``print()``, etc.).
_ERROR_PATTERNS = [
    "ModuleNotFoundError",
    "ImportError",
    "No module named",
    "FileNotFoundError",
    "command not found",
]

# CPython's ModuleNotFoundError prints the module name single-quoted.
# Matching the quoted form (rather than two loose substrings) avoids
# false-positive sibling matches like ``'esphome_dashboard'`` and
# ``'esphome_runtime'`` that share the prefix.
_NO_ESPHOME_MODULE_MARKER = "No module named 'esphome'"

# Progress markers we actually want to surface as job.progress. The
# original wide-open ``\d{1,3}%`` regex matched anything carrying a
# percent sign — including PlatformIO's startup "Unpacking [###] 100%"
# package-extract bar and the post-compile "RAM: 19.3%" / "Flash:
# 80.0%" memory-usage report. Both pinned the bar to non-monotonic
# garbage long before the build's actual progress signal arrived.
# Tightened to a whitelist of three known-real progress shapes:
#
#   * PlatformIO Arduino compile:    ``[ 17%] Compiling foo.cpp.o``
#     The percentage MUST start the line and live inside square
#     brackets so PIO's ESP-IDF builds (which don't emit a per-file
#     percent at all) and the package-extract bar (no ``[NN%]`` shape)
#     never trip it.
#   * esptool serial flash (legacy):  ``Writing at 0x10000... (45 %)``
#     We match a bare parenthesized percentage anywhere in the line:
#     ``(\s*\d{1,3}\s*%\s*\)``. In practice that is enough for the
#     older esptool output shape, and no other expected PIO/ESPHome
#     output uses parens around a bare percentage.
#   * esptool serial flash (current): ``Writing at 0x10000 [bar]  84.8% NNN/NNN bytes...``
#     Newer esptool releases dropped the parens and added an ASCII
#     progress bar plus a decimal percentage. Pinned to the
#     ``Writing at`` prefix so the wider ``\d{1,3}(?:\.\d+)?%``
#     match doesn't fire on memory-usage / unpacking / stray-percent
#     lines elsewhere in the build output. The runner sets
#     ``FORCE_COLOR=1`` / ``CLICOLOR_FORCE=1`` so esptool emits ANSI
#     escape sequences (e.g. ``\x1b[2K`` clear-line) that prefix the
#     output — DO NOT anchor with ``^\s*`` here, those escapes
#     aren't whitespace and the anchor would silently fail in
#     production while passing in plain-text tests. Capture the
#     integer part and discard the decimal — the dashboard's
#     progress bar is a single coarse 0-100 indicator.
#   * ESPHome OTA upload:            ``Uploading: [====] 100% Done...``
#     Anchored to the ``Uploading:`` prefix.
_PROGRESS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*\[\s*(\d{1,3})\s*%\s*\]"),
    re.compile(r"\(\s*(\d{1,3})\s*%\s*\)"),
    re.compile(r"Writing at\b.*?(\d{1,3})(?:\.\d+)?\s*%"),
    re.compile(r"^\s*Uploading:.*?\b(\d{1,3})\s*%"),
)

# History retention. Bulk operations can spawn dozens of jobs at once;
# we want a useful audit trail without letting the metadata file grow
# without bound.
#   - "Primary" = COMPILE / UPLOAD / INSTALL: dedup'd to the most
#     recent terminal job per device, then capped globally.
#   - "Aux" = CLEAN / RESET_BUILD_ENV: kept in a separate small pool
#     so they don't crowd out the device history.
# Active (queued/running) jobs are exempt from both pools.
_MAX_PRIMARY_TERMINAL_JOBS = 50
_MAX_AUX_TERMINAL_JOBS = 5
_PRIMARY_JOB_TYPES: frozenset[JobType] = frozenset(
    {JobType.COMPILE, JobType.UPLOAD, JobType.INSTALL}
)

# Terminal job states — a job in any of these isn't running and
# isn't waiting to run. Used by ``_mark_job_terminal`` to validate
# its argument and by the prune / clear / restore paths to identify
# completed jobs.
_TERMINAL_JOB_STATUSES: frozenset[JobStatus] = frozenset(
    {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
)

# Lifecycle events that end a follower's tail. Subscribed alongside
# ``JOB_OUTPUT`` in ``follow_job`` so the follower's queue receives
# the terminal sentinel for any of the three terminal states. The
# runner fires exactly one of these per job, matching the
# ``_TERMINAL_JOB_STATUSES`` set above — keeping them as separate
# constants because subscriptions key off ``EventType`` while
# state checks key off ``JobStatus``.
_JOB_TERMINAL_EVENTS: frozenset[EventType] = frozenset(
    {EventType.JOB_COMPLETED, EventType.JOB_FAILED, EventType.JOB_CANCELLED}
)

# Per-job output cap for retained terminal jobs. Compile output for a
# successful build runs ~3-10k lines; the head is mostly toolchain
# noise that's rarely useful once the build finished. Trim
# aggressively once the job lands in a terminal state.
_MAX_OUTPUT_LINES_RETAINED = 2000
# Soft cap on ``job.output`` while a job is *still running*. The
# post-completion trim only fires in the ``finally`` block, so a
# misbehaving build that streams gigabytes of stderr (e.g. an
# external_components fetch in a tight retry loop, an esptool stuck
# on a chatty error) used to grow ``job.output`` without bound and
# OOM the dashboard process before the subprocess ever exited. The
# cap is double the post-completion retention floor so a user
# tailing a live build sees roughly twice the kept window during
# the run before old lines start aging off — generous enough for a
# typical tail-along, tight enough to bound memory at a few MB even
# under adversarial output.
#
# Hysteresis: when the buffer crosses the upper cap we trim down to
# ``_INFLIGHT_TRIM_KEEP`` (the post-completion retention floor),
# leaving a ``cap - keep`` line gap before the next trim fires.
# Trimming exactly to the cap would re-trim on every subsequent
# appended line — each trim is an O(cap) list slice, so at 1M
# lines/sec of adversarial output that becomes billions of element
# copies per second and the runner stalls in the slice instead of
# OOMing. The gap also keeps the user-visible buffer stable for
# ``cap - keep`` lines at a time so a tail viewer doesn't see
# rapid-fire "..." trim notices on every line. Choosing
# ``keep == _MAX_OUTPUT_LINES_RETAINED`` makes the post-completion
# trim a no-op for builds that already triggered the in-flight
# trim — never a second round of context loss.
_MAX_OUTPUT_LINES_INFLIGHT = _MAX_OUTPUT_LINES_RETAINED * 2
_INFLIGHT_TRIM_KEEP = _MAX_OUTPUT_LINES_RETAINED
_OUTPUT_TRIM_NOTICE_PREFIX = "... [output trimmed:"
