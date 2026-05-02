"""Tests for ``_trim_job_output`` and the in-flight output cap.

The post-completion trim has been around since the persisted-firmware-
queue work landed; this file pins down the contract so a future
refactor of the in-flight cap (or the ``keep=`` kwarg) doesn't
silently regress either path.

The mid-run cap is the security-relevant addition — without it, a
build that streams gigabytes of stderr in a tight loop
(``external_components`` fetch retry, an esptool error stuck in a
repeating message) holds every line in memory until the subprocess
exits and only the ``finally``-block trim ever fires. The dashboard
process OOMs first.
"""

from __future__ import annotations

from esphome_device_builder.controllers.firmware import (
    _MAX_OUTPUT_LINES_INFLIGHT,
    _MAX_OUTPUT_LINES_RETAINED,
    _OUTPUT_TRIM_NOTICE_PREFIX,
    _trim_job_output,
)
from esphome_device_builder.models import FirmwareJob, JobStatus, JobType


def _job(lines: int) -> FirmwareJob:
    return FirmwareJob(
        job_id="abc123",
        configuration="kitchen.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.RUNNING,
        output=[f"line {i}\n" for i in range(lines)],
    )


# ----------------------------------------------------------------------
# Default (post-completion) trim
# ----------------------------------------------------------------------


def test_trim_below_retained_cap_is_noop() -> None:
    """Buffer at or under the retention cap is left alone."""
    job = _job(_MAX_OUTPUT_LINES_RETAINED)
    before = list(job.output)

    _trim_job_output(job)

    assert job.output == before


def test_trim_drops_head_and_prepends_elided_notice() -> None:
    """Above the cap → keep the tail, prepend a "<N> earlier elided" line."""
    excess = 100
    job = _job(_MAX_OUTPUT_LINES_RETAINED + excess)

    _trim_job_output(job)

    # +1 for the elided-notice prepended at the head.
    assert len(job.output) == _MAX_OUTPUT_LINES_RETAINED + 1
    assert job.output[0].startswith(_OUTPUT_TRIM_NOTICE_PREFIX)
    assert f"{excess} earlier" in job.output[0]
    # Tail preserved verbatim.
    assert job.output[-1] == f"line {_MAX_OUTPUT_LINES_RETAINED + excess - 1}\n"


def test_trim_is_idempotent_when_called_again() -> None:
    """Re-trimming an already-trimmed buffer doesn't bump the elided count."""
    job = _job(_MAX_OUTPUT_LINES_RETAINED + 100)

    _trim_job_output(job)
    snapshot = list(job.output)
    _trim_job_output(job)

    assert job.output == snapshot


def test_trim_folds_elided_count_across_repeated_trims() -> None:
    """Two distinct trim cycles → cumulative elided count, not just the latest.

    Catches a regression where the second trim would report only its
    own dropped lines and pretend the first trim never happened.
    """
    job = _job(_MAX_OUTPUT_LINES_RETAINED + 100)
    _trim_job_output(job)  # drops 100

    # Append more lines, push past the cap again.
    job.output.extend(f"more {i}\n" for i in range(200))
    _trim_job_output(job)  # drops another 200

    assert job.output[0].startswith(_OUTPUT_TRIM_NOTICE_PREFIX)
    assert "300 earlier" in job.output[0], (
        f"expected cumulative count of 300, got: {job.output[0]!r}"
    )


# ----------------------------------------------------------------------
# In-flight cap (security-relevant: bounds mid-run memory growth)
# ----------------------------------------------------------------------


def test_inflight_cap_is_higher_than_retention_cap() -> None:
    """Sanity: in-flight cap leaves headroom over the retention floor.

    A user tailing a live build wouldn't appreciate the buffer
    snapping back to 2000 lines every few seconds, so the in-flight
    cap is intentionally larger. Lock the relationship in place so a
    future tweak doesn't accidentally invert it.
    """
    assert _MAX_OUTPUT_LINES_INFLIGHT > _MAX_OUTPUT_LINES_RETAINED


def test_trim_with_keep_inflight_preserves_more_lines() -> None:
    """Passing ``keep=_MAX_OUTPUT_LINES_INFLIGHT`` keeps the tail at that size."""
    excess = 1000
    job = _job(_MAX_OUTPUT_LINES_INFLIGHT + excess)

    _trim_job_output(job, keep=_MAX_OUTPUT_LINES_INFLIGHT)

    assert len(job.output) == _MAX_OUTPUT_LINES_INFLIGHT + 1
    assert f"{excess} earlier" in job.output[0]
    # Last line of the original buffer survives.
    assert job.output[-1] == f"line {_MAX_OUTPUT_LINES_INFLIGHT + excess - 1}\n"


def test_inflight_trim_followed_by_default_trim_chains_elided_counts() -> None:
    """Mid-run trim → terminal trim: both contributions counted.

    Mirrors the production flow: streaming loop trims the buffer
    when it crosses the in-flight cap, then ``_trim_job_output`` is
    called again in the ``finally`` block with the default
    (smaller) retention cap.
    """
    job = _job(_MAX_OUTPUT_LINES_INFLIGHT + 100)

    # Mid-run trim: drops 100 lines (above the in-flight cap).
    _trim_job_output(job, keep=_MAX_OUTPUT_LINES_INFLIGHT)
    # Post-completion trim: drops the difference between in-flight
    # and retention caps.
    _trim_job_output(job)

    assert len(job.output) == _MAX_OUTPUT_LINES_RETAINED + 1
    assert job.output[0].startswith(_OUTPUT_TRIM_NOTICE_PREFIX)
    expected_total = 100 + (_MAX_OUTPUT_LINES_INFLIGHT - _MAX_OUTPUT_LINES_RETAINED)
    assert f"{expected_total} earlier" in job.output[0]
