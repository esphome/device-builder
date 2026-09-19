"""Tests for ``follow.job_report`` and ``FirmwareState.dependents``."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from esphome_device_builder.controllers.firmware._state import FirmwareState
from esphome_device_builder.controllers.firmware.follow import job_report
from esphome_device_builder.models import JobStatus, JobType
from tests.conftest import make_job

_READ = "esphome_device_builder.controllers.firmware.follow.read_job_output"


@pytest.mark.parametrize(
    ("tail_lines", "output", "truncated"),
    [(2, ["1", "2"], True), (5, ["0", "1", "2"], False), (0, [], True)],
    ids=["tail", "whole_log", "no_lines"],
)
async def test_job_report_tails_the_live_output(
    tail_lines: int, output: list[str], truncated: bool
) -> None:
    job = make_job(output=["0\n", "1\n", "2\n"])

    report = await job_report(job, tail_lines=tail_lines)

    assert report["job_id"] == job.job_id
    assert report["output"] == output
    assert report["truncated"] is truncated
    assert report["output_available"] is True
    assert report["queued_update_armed"] is False


async def test_job_report_reports_an_armed_queued_update() -> None:
    job = make_job(
        job_type=JobType.UPLOAD,
        port="OTA",
        status=JobStatus.FAILED,
        is_deferred_install=True,
    )

    with patch(_READ, return_value=None):
        report = await job_report(job, tail_lines=10)

    assert report["queued_update_armed"] is True


async def test_job_report_cleans_concealed_values_and_colour() -> None:
    job = make_job(output=["\x1b[32mpassword: \x1b[8mhunter2secret\x1b[28m\x1b[0m\n"])

    report = await job_report(job, tail_lines=10)

    assert report["output"] == ["password: <removed>"]


@pytest.mark.parametrize(
    ("sidecar", "output", "available"),
    [(["built\n"], ["built"], True), (None, [], False)],
    ids=["sidecar", "unreadable"],
)
async def test_job_report_reads_a_terminal_job_sidecar(
    sidecar: list[str] | None, output: list[str], available: bool
) -> None:
    job = make_job(status=JobStatus.COMPLETED, exit_code=0)

    with patch(_READ, return_value=sidecar):
        report = await job_report(job, tail_lines=10)

    assert report["output"] == output
    assert report["output_available"] is available


def test_dependents_yields_the_jobs_held_on_another() -> None:
    state = FirmwareState()
    compile_job = make_job(job_id="compile")
    upload_job = make_job(job_id="upload", depends_on="compile")
    state.jobs.update({j.job_id: j for j in (compile_job, upload_job)})

    assert list(state.dependents("compile")) == [upload_job]
    assert list(state.dependents("upload")) == []
