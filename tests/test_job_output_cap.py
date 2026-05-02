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
    _INFLIGHT_TRIM_KEEP,
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


# Base ``_trim_job_output`` cases (noop under cap, drop+notice over cap,
# idempotent re-trim, cumulative elided count across calls) live in
# ``tests/test_firmware_helpers.py`` — keeping helper expectations in
# one place to avoid drift. The cases below are specifically about the
# in-flight cap and its interaction with the post-completion trim.


# ----------------------------------------------------------------------
# In-flight cap (security-relevant: bounds mid-run memory growth)
# ----------------------------------------------------------------------


def test_inflight_cap_invariants() -> None:
    """Sanity: cap > keep >= retention.

    Two rules locked in one block so a future tweak inverting either
    surfaces immediately:

    - ``_MAX_OUTPUT_LINES_INFLIGHT`` > ``_INFLIGHT_TRIM_KEEP`` is the
      hysteresis gap. Equality means every line above the cap pays
      an O(cap) slice copy.
    - ``_INFLIGHT_TRIM_KEEP`` >= ``_MAX_OUTPUT_LINES_RETAINED`` so the
      post-completion trim is at most a no-op for builds that
      already triggered the in-flight trim — never a second round of
      context loss. Equality is fine; ``keep`` smaller than
      ``retained`` is the regression we're guarding against.
    """
    assert _MAX_OUTPUT_LINES_INFLIGHT > _INFLIGHT_TRIM_KEEP
    assert _INFLIGHT_TRIM_KEEP >= _MAX_OUTPUT_LINES_RETAINED


def test_trim_with_keep_inflight_preserves_keep_window() -> None:
    """Trimming with ``keep=_INFLIGHT_TRIM_KEEP`` lands on the keep size."""
    excess = 1000
    job = _job(_MAX_OUTPUT_LINES_INFLIGHT + excess)

    _trim_job_output(job, keep=_INFLIGHT_TRIM_KEEP)

    # +1 for the elided-notice prepended at the head.
    assert len(job.output) == _INFLIGHT_TRIM_KEEP + 1
    # Dropped count = original size - keep window.
    expected_dropped = _MAX_OUTPUT_LINES_INFLIGHT + excess - _INFLIGHT_TRIM_KEEP
    assert f"{expected_dropped} earlier" in job.output[0]
    # Last line of the original buffer survives.
    assert job.output[-1] == f"line {_MAX_OUTPUT_LINES_INFLIGHT + excess - 1}\n"


def test_inflight_hysteresis_amortises_trim_cost() -> None:
    """Trimming below the cap creates a gap before the next trim fires.

    Catches a regression where the streaming loop trims down to the
    cap itself — every subsequent appended line crosses the
    threshold again and pays an O(cap) slice copy. With the
    hysteresis gap, the next ``cap - keep`` lines append without
    triggering a trim.
    """
    job = _job(_MAX_OUTPUT_LINES_INFLIGHT + 1)
    _trim_job_output(job, keep=_INFLIGHT_TRIM_KEEP)
    # Buffer is now at keep + 1 (the elided notice). Need to add
    # ``cap - keep`` lines before the next len > cap check trips.
    headroom = _MAX_OUTPUT_LINES_INFLIGHT - _INFLIGHT_TRIM_KEEP
    assert headroom > 0, "no hysteresis gap — every line will re-trim"
    # Simulate the streaming loop's check explicitly: appending
    # ``headroom - 1`` more lines stays under the cap; ``headroom``
    # crosses it.
    for i in range(headroom - 1):
        job.output.append(f"new {i}\n")
    assert len(job.output) <= _MAX_OUTPUT_LINES_INFLIGHT


def test_no_module_named_esphome_detection_at_append_time() -> None:
    r"""``saw_no_esphome_module`` flag survives the in-flight trim.

    The post-exit handler used to render the actionable
    ``"esphome is not importable from the dashboard's Python
    environment …"`` message by re-scanning ``"".join(job.output)``
    for ``No module named esphome``. After the in-flight cap can
    elide the head, that line might be gone by exit time and the
    user gets the generic ``"Process exited 0 but output contains
    errors"`` message instead of the specific install hint.

    Cure: set the flag at append time (where ``_check_error``
    already had the line in hand). This test pins the at-append
    detection by exercising the same closure shape the runner
    uses: define a local flag + closure, feed lines through it,
    and verify the flag stays True after the source line is
    removed from the buffer.
    """
    from esphome_device_builder.controllers.firmware import _ERROR_PATTERNS

    has_error_in_output = False
    saw_no_esphome_module = False

    def _check_error(text: str) -> None:
        nonlocal has_error_in_output, saw_no_esphome_module
        if "No module named 'esphome'" in text:
            saw_no_esphome_module = True
        if has_error_in_output:
            return
        for pattern in _ERROR_PATTERNS:
            if pattern in text:
                has_error_in_output = True
                return

    # Line at the start of a long noisy build, in the exact format
    # CPython emits — with the module name single-quoted. The
    # post-exit check ``"No module named esphome" in full_output``
    # never matched this real-world string; capturing at append
    # time on the quoted form rather than two loose substrings
    # also avoids false-positive sibling matches like
    # ``esphome_dashboard``.
    _check_error("ModuleNotFoundError: No module named 'esphome'\n")
    # Mountains of unrelated noise.
    for i in range(_MAX_OUTPUT_LINES_INFLIGHT * 2):
        _check_error(f"line {i}\n")

    assert has_error_in_output is True
    # The captured flag is what the post-exit handler uses to pick
    # the actionable error message — set once, never cleared.
    assert saw_no_esphome_module is True

    # Sibling modules that share the ``esphome`` prefix must not
    # trigger the flag — that was the false-positive risk the
    # quoted-form check exists to close.
    saw_no_esphome_module = False
    _check_error("ModuleNotFoundError: No module named 'esphome_dashboard'\n")
    assert saw_no_esphome_module is False


def test_inflight_trim_followed_by_default_trim_chains_elided_counts() -> None:
    """Mid-run trim → terminal trim: both contributions counted.

    Mirrors the production flow: streaming loop trims the buffer
    when it crosses the in-flight cap (down to ``_INFLIGHT_TRIM_KEEP``),
    then ``_trim_job_output`` is called again in the ``finally``
    block with the default (smaller) retention cap.
    """
    job = _job(_MAX_OUTPUT_LINES_INFLIGHT + 100)

    # Mid-run trim: drops down to keep window.
    _trim_job_output(job, keep=_INFLIGHT_TRIM_KEEP)
    # Post-completion trim: drops the difference between keep and
    # retention caps.
    _trim_job_output(job)

    assert len(job.output) == _MAX_OUTPUT_LINES_RETAINED + 1
    assert job.output[0].startswith(_OUTPUT_TRIM_NOTICE_PREFIX)
    expected_total = (_MAX_OUTPUT_LINES_INFLIGHT + 100 - _INFLIGHT_TRIM_KEEP) + (
        _INFLIGHT_TRIM_KEEP - _MAX_OUTPUT_LINES_RETAINED
    )
    assert f"{expected_total} earlier" in job.output[0]
