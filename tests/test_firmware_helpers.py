"""Tests for module-level helpers in ``controllers/firmware.py``.

The firmware controller has a few pure helpers at file scope
that aren't covered elsewhere:

* ``_trim_job_output`` — caps ``job.output`` and accumulates
  the elided count across repeated trims.
* ``_names_touched_by_job`` — feeds the rename-lock collision
  check; a rename touches two YAMLs (old + new), every other
  job type touches one.

The other module-level helpers are already covered by their
own dedicated test files:

* ``_validate_port`` → ``test_install_to_specific_address.py``
* ``_parse_progress`` → ``test_firmware_progress.py``
* ``_mark_job_terminal`` → ``test_mark_job_terminal.py``

Per Copilot's review, this PR doesn't re-cover those — keeping
expectations in one place avoids drift.
"""

from __future__ import annotations

import re
from typing import Any

from esphome_device_builder.controllers.firmware import (
    _MAX_OUTPUT_LINES_RETAINED,
    _OUTPUT_TRIM_NOTICE_PREFIX,
    _names_touched_by_job,
    _trim_job_output,
)
from esphome_device_builder.models.firmware import (
    FirmwareJob,
    JobType,
)


def _make_job(**overrides: Any) -> FirmwareJob:
    """Minimal FirmwareJob — only the fields the helpers under test read."""
    defaults: dict[str, Any] = {
        "job_id": "j-1",
        "configuration": "kitchen.yaml",
        "job_type": JobType.COMPILE,
    }
    defaults.update(overrides)
    return FirmwareJob(**defaults)


# ---------------------------------------------------------------------------
# _trim_job_output
# ---------------------------------------------------------------------------


def test_trim_job_output_no_op_when_under_cap() -> None:
    """Below the cap → output untouched, no trim notice prepended."""
    job = _make_job(output=["line\n"] * 10)
    _trim_job_output(job)
    assert len(job.output) == 10
    assert not any(line.startswith(_OUTPUT_TRIM_NOTICE_PREFIX) for line in job.output)


def test_trim_job_output_caps_long_output() -> None:
    """Above the cap → output trimmed to the most recent N lines plus notice.

    Cap is the constant ``_MAX_OUTPUT_LINES_RETAINED`` so the
    test scales with the source. The trim notice goes in slot 0
    so the user sees "X lines elided" before the kept tail.
    """
    job = _make_job(output=[f"line {i}\n" for i in range(_MAX_OUTPUT_LINES_RETAINED + 50)])
    _trim_job_output(job)

    # Notice + cap == total length.
    assert len(job.output) == _MAX_OUTPUT_LINES_RETAINED + 1
    assert job.output[0].startswith(_OUTPUT_TRIM_NOTICE_PREFIX)
    assert "50 earlier line(s) elided" in job.output[0]
    # Tail kept — last line is the most recent.
    assert job.output[-1] == f"line {_MAX_OUTPUT_LINES_RETAINED + 49}\n"


def test_trim_job_output_accumulates_elided_count_across_calls() -> None:
    """Repeated trims grow the elided count instead of resetting to 1.

    The trim notice carries the cumulative count so a long-
    running job that gets trimmed multiple times reports the
    true total ("1234 earlier lines elided") instead of falsely
    claiming it just dropped one batch.
    """
    job = _make_job(output=[f"line {i}\n" for i in range(_MAX_OUTPUT_LINES_RETAINED + 30)])
    _trim_job_output(job)
    first_count = int(re.search(r"(\d+) earlier", job.output[0]).group(1))  # type: ignore[union-attr]

    # Append more output and trim again.
    job.output.extend(f"line {i}\n" for i in range(50))
    _trim_job_output(job)
    second_count = int(re.search(r"(\d+) earlier", job.output[0]).group(1))  # type: ignore[union-attr]

    assert second_count > first_count
    # The new count should be first + new lines elided this round.
    assert second_count == first_count + 50


# ---------------------------------------------------------------------------
# _names_touched_by_job
# ---------------------------------------------------------------------------


def test_names_touched_by_compile_job_is_just_configuration() -> None:
    """Compile / upload / install / clean each touch one YAML.

    The rename-lock collision check uses this set to decide
    whether two queued jobs can run in parallel. A compile of
    ``kitchen.yaml`` only has ``kitchen.yaml`` in its working
    set.
    """
    job = _make_job(configuration="kitchen.yaml", job_type=JobType.COMPILE)
    assert _names_touched_by_job(job) == {"kitchen.yaml"}


def test_names_touched_by_rename_includes_old_and_new() -> None:
    """A rename collides on both the source and the target YAML.

    Without the second name, a queued compile of the *new* name
    could start before the rename's install lands and fight
    over the same StorageJSON sidecar.
    """
    job = _make_job(
        configuration="kitchen.yaml",
        job_type=JobType.RENAME,
        new_name="kitchen-2",
    )
    assert _names_touched_by_job(job) == {"kitchen.yaml", "kitchen-2.yaml"}


def test_names_touched_by_rename_without_new_name_falls_back() -> None:
    """A rename job missing ``new_name`` only locks the source.

    Defensive: an enqueue that didn't fill ``new_name`` (test
    fixture, paranoid caller) shouldn't blow up the lock-check
    helper. Falling back to the source-only set means the
    collision detector still runs sensibly.
    """
    job = _make_job(configuration="kitchen.yaml", job_type=JobType.RENAME)
    assert _names_touched_by_job(job) == {"kitchen.yaml"}


def test_names_touched_by_job_with_empty_configuration_is_empty() -> None:
    """Reset-build-env-style jobs have no configuration → empty set.

    ``reset_build_env`` operates on the platformio cache, not a
    specific YAML. The empty set says "doesn't conflict with
    anything", which is the desired behaviour.
    """
    job = _make_job(configuration="", job_type=JobType.RESET_BUILD_ENV)
    assert _names_touched_by_job(job) == set()
