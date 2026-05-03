"""Tests for module-level helpers in ``controllers/firmware/helpers.py``.

The firmware controller has a few pure helpers at file scope
that aren't covered elsewhere:

* ``_trim_job_output`` — caps ``job.output`` and accumulates
  the elided count across repeated trims.
* ``_names_touched_by_job`` — feeds the rename-lock collision
  check; a rename touches two YAMLs (old + new), every other
  job type touches one.
* ``_verify_esphome_importable`` — startup probe, returns
  ``(True, version)`` on success and ``(False, reason)`` on
  exit-code failure / error-pattern detection / OSError /
  timeout.

The other module-level helpers are already covered by their
own dedicated test files:

* ``_validate_port`` → ``test_install_to_specific_address.py``
* ``_parse_progress`` → ``test_progress.py``
* ``_mark_job_terminal`` → ``test_mark_job_terminal.py``

Per Copilot's review, this PR doesn't re-cover those — keeping
expectations in one place avoids drift.
"""

from __future__ import annotations

import re
import sys
from typing import Any

import pytest

from esphome_device_builder.controllers.firmware.constants import (
    _MAX_OUTPUT_LINES_RETAINED,
    _OUTPUT_TRIM_NOTICE_PREFIX,
)
from esphome_device_builder.controllers.firmware.helpers import (
    _names_touched_by_job,
    _reset_job_for_recovery,
    _trim_job_output,
    _verify_esphome_importable,
)
from esphome_device_builder.models.firmware import (
    FirmwareJob,
    JobStatus,
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


# ---------------------------------------------------------------------------
# _verify_esphome_importable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_esphome_importable_success_with_known_module() -> None:
    """A trivial Python ``-c`` that prints its version returns ``(True, output)``.

    Exercises the spawn path against a known-importable command —
    we don't need the real ``esphome`` CLI for this; a one-liner
    that exits 0 with no error patterns is enough to lock the
    happy-path tuple shape.
    """
    cmd = [sys.executable, "-c", "print('1.2.3')"]
    ok, detail = await _verify_esphome_importable(cmd)
    assert ok
    assert "1.2.3" in detail


@pytest.mark.asyncio
async def test_verify_esphome_importable_returns_false_on_no_module_named() -> None:
    """Even on a 0 exit, output containing ``No module named`` flips the result.

    Captures the case where a wrapper script exits 0 but its
    stderr/stdout still complains about a missing module — the
    historical class of failure that motivated this probe.
    """
    cmd = [sys.executable, "-c", "import sys; print(\"No module named 'esphome'\"); sys.exit(0)"]
    ok, detail = await _verify_esphome_importable(cmd)
    assert not ok
    assert "No module named" in detail


@pytest.mark.asyncio
async def test_verify_esphome_importable_returns_false_on_nonzero_exit() -> None:
    """Non-zero exit → ``(False, output_or_exit_marker)``."""
    cmd = [sys.executable, "-c", "import sys; sys.exit(3)"]
    ok, detail = await _verify_esphome_importable(cmd)
    assert not ok
    assert "exit 3" in detail


@pytest.mark.asyncio
async def test_verify_esphome_importable_returns_false_on_oserror() -> None:
    """A missing executable returns ``(False, "FileNotFoundError: ...")``.

    Pre-migration the sync version caught ``OSError`` directly;
    the async version uses the same except branch around
    ``create_subprocess_exec``.
    """
    cmd = ["/this/path/does/not/exist/no-such-binary"]
    ok, detail = await _verify_esphome_importable(cmd)
    assert not ok
    assert "FileNotFoundError" in detail or "OSError" in detail


# ---------------------------------------------------------------------------
# _reset_job_for_recovery
# ---------------------------------------------------------------------------


def test_reset_job_for_recovery_keeps_log_and_appends_marker() -> None:
    """The pre-crash log survives, with a separator marker appended.

    The build log is useful diagnostic history for "what was
    happening when the dashboard died"; clearing it on recovery
    would lose that. A marker line lets a follower see exactly
    where the rebuild's output starts in the merged buffer.
    """
    job = _make_job(
        status=JobStatus.RUNNING,
        output=["compile in progress\n", "src/main.cpp\n"],
        progress=42,
    )

    _reset_job_for_recovery(job)

    assert job.output[:2] == ["compile in progress\n", "src/main.cpp\n"]
    assert any("dashboard restarted mid-build" in line for line in job.output)
    assert job.output[-1].endswith("\n")


def test_reset_job_for_recovery_clears_per_run_state() -> None:
    """Per-run state fields reset to their defaults so the rebuild looks fresh.

    Without this, a follower attached to the re-run would see
    the pre-crash ``progress`` / ``exit_code`` / ``started_at``
    leak into the rebuild's status display before the new run
    overwrites them.
    """
    job = _make_job(
        status=JobStatus.RUNNING,
        progress=47,
        error="prior partial error",
        started_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:01:00+00:00",
        exit_code=1,
    )

    _reset_job_for_recovery(job)

    assert job.progress is None
    assert job.error is None
    assert job.started_at is None
    assert job.completed_at is None
    assert job.exit_code is None


def test_reset_job_for_recovery_does_not_change_status() -> None:
    """The status flip is the caller's responsibility.

    ``_reset_job_for_recovery`` is a state-cleaner; the load
    path's transition (``RUNNING`` → ``QUEUED``) lives at the
    call site so future callers that wanted a different
    transition don't have to fight a hardcoded one.
    """
    job = _make_job(status=JobStatus.RUNNING)
    _reset_job_for_recovery(job)
    assert job.status == JobStatus.RUNNING


def test_reset_job_for_recovery_preserves_job_identity() -> None:
    """Configuration / job_type / created_at / port / new_name stay intact.

    These describe the job, not the run. A user submitting
    ``rename kitchen → livingroom`` and then crashing should
    have the rebuild target the same rename, not lose the
    new_name and re-run as a vanilla compile.
    """
    job = _make_job(
        status=JobStatus.RUNNING,
        configuration="kitchen.yaml",
        job_type=JobType.RENAME,
        new_name="livingroom",
        created_at="2025-12-31T23:59:59+00:00",
    )
    job.port = "/dev/ttyUSB0"

    _reset_job_for_recovery(job)

    assert job.configuration == "kitchen.yaml"
    assert job.job_type == JobType.RENAME
    assert job.new_name == "livingroom"
    assert job.port == "/dev/ttyUSB0"
    assert job.created_at == "2025-12-31T23:59:59+00:00"
