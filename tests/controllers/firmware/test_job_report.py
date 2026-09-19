"""Tests for ``follow.job_report`` and ``jobs.dependent_job``."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import pytest

from esphome_device_builder.controllers.firmware import FirmwareController
from esphome_device_builder.controllers.firmware.follow import job_report
from esphome_device_builder.controllers.firmware.jobs import dependent_job
from esphome_device_builder.models import JobStatus
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
    assert report["queued_update_armed"] is job.is_queued_update_armed


async def test_job_report_cleans_concealed_values_and_colour() -> None:
    job = make_job(output=["\x1b[32mpassword: \x1b[8mhunter2secret\x1b[28m\x1b[0m\n"])

    report = await job_report(job, tail_lines=10)

    assert report["output"] == ["password: <removed>"]


@pytest.mark.parametrize(
    ("sidecar", "output", "available"),
    [(["built\n"], ["built"], True), (None, [], False)],
    ids=["sidecar", "unreadable"],
)
async def test_job_report_reads_a_terminal_jobs_sidecar(
    sidecar: list[str] | None, output: list[str], available: bool
) -> None:
    job = make_job(status=JobStatus.COMPLETED, exit_code=0)

    with patch(_READ, return_value=sidecar):
        report = await job_report(job, tail_lines=10)

    assert report["output"] == output
    assert report["output_available"] is available


def test_dependent_job_finds_the_job_held_on_another() -> None:
    compile_job = make_job(job_id="compile")
    upload_job = make_job(job_id="upload", depends_on="compile")
    jobs = {j.job_id: j for j in (compile_job, upload_job)}
    controller = cast("FirmwareController", SimpleNamespace(state=SimpleNamespace(jobs=jobs)))

    assert dependent_job(controller, "compile") is upload_job
    assert dependent_job(controller, "upload") is None
