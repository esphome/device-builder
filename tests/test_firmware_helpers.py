"""Tests for the module-level helpers in ``controllers/firmware.py``.

The firmware controller is the largest file in the package
(~1500 lines, 34% covered), so this PR carves out a focused
slice: the pure functions at file scope. They're easy to test,
they get called from every job and every WS command, and they
encode rules that have already burned us once.

Targets:

* ``_validate_port`` — the WS-side gate for ``--device``. Must
  accept every shape the esphome CLI does (empty, ``"OTA"``,
  serial paths across POSIX / Windows, IPv4, IPv6, hostnames
  including trailing-dot FQDNs from zeroconf) and reject anything
  obviously broken (random punctuation, truncated IPs).
* ``_parse_progress`` — pulls a 0-100 percentage out of build /
  flash output. Stray ``%`` signs in unrelated output (memory
  reports, "Unpacking…") must not be misread as progress.
* ``_trim_job_output`` — keeps job.output bounded; the elided
  count must accumulate across repeated trims (otherwise long
  jobs would always claim "1 line elided" and lose accounting).
* ``_mark_job_terminal`` — pairs a terminal status with a
  ``completed_at`` stamp; refuses non-terminal statuses.
* ``_names_touched_by_job`` — feeds the rename-lock collision
  check; a rename has two YAMLs (old + new), every other job
  type has one.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from esphome_device_builder.controllers.firmware import (
    _MAX_OUTPUT_LINES_RETAINED,
    _OUTPUT_TRIM_NOTICE_PREFIX,
    _mark_job_terminal,
    _names_touched_by_job,
    _parse_progress,
    _trim_job_output,
    _validate_port,
)
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode
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
# _validate_port
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "port",
    [
        "",
        "OTA",
        "/dev/ttyUSB0",
        "/dev/ttyACM1",
        "/dev/cu.usbserial-1410",
        "COM3",  # Windows
        "192.168.1.42",
        "10.0.0.1",
        "::1",
        "fe80::1",
        "kitchen.local",
        "kitchen.local.",  # trailing-dot FQDN from zeroconf
        "device.example.com",
        "device.example.com.",
        "kitchen",
        "esp32-c6-devkit",
    ],
)
def test_validate_port_accepts_known_shapes(port: str) -> None:
    """Every shape the esphome CLI accepts must pass.

    Catalogues the full positive surface so a regex tightening
    that rejects e.g. trailing-dot FQDNs (which zeroconf
    routinely produces) breaks the test instead of silently
    making mDNS-resolved addresses unusable.
    """
    _validate_port(port)  # no raise


@pytest.mark.parametrize(
    "port",
    [
        "192.168.1",  # truncated IPv4 — IP shape, not IP
        "256.256.256.256",  # invalid octets
        "1.2.3.4.5",  # too many octets
        "not a host",  # space in hostname
        "host_with_underscore",  # underscores not in RFC 1123
        "-leading-dash.local",  # leading dash on a label
        "label..double",  # empty label
        "host!",  # punctuation
        "https://kitchen.local",  # URL, not host
    ],
)
def test_validate_port_rejects_garbage(port: str) -> None:
    """Anything outside the accepted shapes raises INVALID_ARGS.

    The error message is surfaced verbatim over WS, so the
    frontend can pattern-match on the wording — keep the
    INVALID_ARGS code stable here even when the message
    changes.
    """
    with pytest.raises(CommandError) as exc:
        _validate_port(port)
    assert exc.value.code == ErrorCode.INVALID_ARGS


def test_validate_port_ip_shaped_failure_includes_parse_error() -> None:
    """A would-be IP (``looks_ip``) reports the underlying parse error.

    Keeps the message specific enough that a typo'd IP gets the
    "looks like an IP" hint instead of the generic "expected …"
    catch-all. Helps users notice they truncated an octet.
    """
    with pytest.raises(CommandError) as exc:
        _validate_port("192.168.1")
    assert "looks like an IP" in str(exc.value.message)


# ---------------------------------------------------------------------------
# _parse_progress
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("[ 12%] Building .pio/build/...", 12),
        ("[100%] Linking .pio/build/firmware.elf", 100),
        ("Compiling foo.cpp (45%)", 45),
        ("Uploading: [=======     ] 33%", 33),
        ("Random output without percent", None),
        ("RAM: 8.5% (used 28000 bytes...)", None),  # RAM/flash usage
        ("Unpacking ##########", None),
    ],
)
def test_parse_progress_recognises_known_shapes(line: str, expected: int | None) -> None:
    """The three patterns cover ESP-IDF, PlatformIO, and esptool output.

    Stray ``%`` signs in memory-usage reports must not be
    misread as build progress — otherwise the dashboard's
    progress bar would briefly jump to "8%" partway through a
    successful compile while still resolving the IDF cache, and
    snap back to wherever the actual progress lands. The
    ``RAM: 8.5%`` test pins the regression.
    """
    assert _parse_progress(line) == expected


def test_parse_progress_clamps_out_of_range() -> None:
    """Values outside 0-100 don't get returned.

    The pattern matches loosely (1-3 digits); a 3-digit garbage
    value (``[ 234%]``) shouldn't propagate to the dashboard's
    progress bar where it would render past the right edge.
    """
    assert _parse_progress("[234%] noise") is None


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
# _mark_job_terminal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED],
)
def test_mark_job_terminal_stamps_completion(status: JobStatus) -> None:
    """Terminal status + ``completed_at`` are written together.

    Forgetting either is the recurring footgun the helper
    exists to prevent. Pin both writes so a refactor that
    splits them breaks the test.
    """
    job = _make_job(status=JobStatus.RUNNING, started_at="2026-01-01T00:00:00+00:00")
    _mark_job_terminal(job, status)
    assert job.status == status
    assert job.completed_at is not None
    # ISO 8601 with timezone offset (UTC).
    assert job.completed_at.endswith("+00:00") or job.completed_at.endswith("Z")


@pytest.mark.parametrize(
    "status",
    [JobStatus.QUEUED, JobStatus.RUNNING],
)
def test_mark_job_terminal_rejects_non_terminal_status(status: JobStatus) -> None:
    """Non-terminal statuses raise ``ValueError``.

    Stamping ``completed_at`` on a still-running job mis-orders
    the dashboard's relative-time strings and confuses the
    prune-on-shutdown logic. The raise is the loud-fail that
    catches such misuse early.
    """
    job = _make_job(status=JobStatus.RUNNING)
    with pytest.raises(ValueError, match="non-terminal"):
        _mark_job_terminal(job, status)


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
