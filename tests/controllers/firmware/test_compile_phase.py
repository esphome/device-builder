"""``compile_started_at`` / ``compile_ended_at`` stamping.

The compile clock must count compilation only: it starts on the first line
that proves the toolchain is building (a parseable progress percent, or a
PlatformIO ``Compiling`` word marker the percent parser can't see), and stops
on the summary banner — never during the dependency download or CMake
configure, and never counting an install's flash phase.
"""

from __future__ import annotations

import pytest

from esphome_device_builder.controllers.firmware.helpers import _stamp_compile_phase
from esphome_device_builder.models.firmware import FirmwareJob, JobType


def _job() -> FirmwareJob:
    return FirmwareJob(job_id="j", configuration="c.yaml", job_type=JobType.COMPILE)


class TestCompileStart:
    @pytest.mark.parametrize(
        "line",
        [
            "Compiling .pio/build/esp32dev/src/main.cpp.o",
            "Compiling .pioenvs/apy/esp_hw_support/cpu.c.o",
            "Archiving .pio/build/nodemcuv2/libFrameworkArduino.a",
            "Linking .pio/build/bk72xx/firmware.elf",
            "Building in release mode",
        ],
    )
    def test_word_marker_starts_without_percent(self, line: str) -> None:
        job = _job()
        _stamp_compile_phase(job, line, None)
        assert job.compile_started_at is not None

    def test_parsed_progress_starts_for_raw_ninja(self) -> None:
        # esp-idf ninja prints no "Compiling" word; the counter is caught by
        # progress parsing, which is the start signal here.
        job = _job()
        _stamp_compile_phase(job, "[907/1424] Building C object foo.c.obj", 63)
        assert job.compile_started_at is not None

    @pytest.mark.parametrize(
        "line",
        [
            "Tool Manager: Installing framework-arduinoespressif32",
            "Library Manager: Installing esphome/noise-c @ 0.1.11",
            "Unpacking [####################] 100%",
            "-- Configuring done (3.0s)",
            "Executing action: reconfigure",
            "[1/2] Re-running CMake...",
        ],
    )
    def test_download_and_configure_do_not_start(self, line: str) -> None:
        job = _job()
        _stamp_compile_phase(job, line, None)
        assert job.compile_started_at is None

    def test_start_is_latched_once(self) -> None:
        job = _job()
        _stamp_compile_phase(job, "Compiling a.cpp.o", None)
        first = job.compile_started_at
        _stamp_compile_phase(job, "Compiling b.cpp.o", None)
        assert job.compile_started_at == first


class TestCompileEnd:
    @pytest.mark.parametrize(
        "line",
        [
            "===================== [SUCCESS] Took 15.36 seconds =====================",
            "===================== [FAILED] Took 4.10 seconds =====================",
        ],
    )
    def test_banner_ends_after_start(self, line: str) -> None:
        job = _job()
        _stamp_compile_phase(job, "Compiling a.cpp.o", None)
        _stamp_compile_phase(job, line, None)
        assert job.compile_ended_at is not None

    def test_end_ignored_before_start(self) -> None:
        job = _job()
        _stamp_compile_phase(job, "[SUCCESS] Took 1.0 seconds", None)
        assert job.compile_started_at is None
        assert job.compile_ended_at is None

    def test_end_is_latched_once(self) -> None:
        job = _job()
        _stamp_compile_phase(job, "Compiling a.cpp.o", None)
        _stamp_compile_phase(job, "[SUCCESS] Took 1.0 seconds", None)
        first = job.compile_ended_at
        _stamp_compile_phase(job, "[FAILED] Took 9.0 seconds", None)
        assert job.compile_ended_at == first


def test_cleared_by_clear_run_state() -> None:
    job = _job()
    _stamp_compile_phase(job, "Compiling a.cpp.o", None)
    _stamp_compile_phase(job, "[SUCCESS] Took 1.0 seconds", None)
    job.clear_run_state()
    assert job.compile_started_at is None
    assert job.compile_ended_at is None
