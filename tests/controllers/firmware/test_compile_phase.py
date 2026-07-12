"""``compile_started_at`` / ``compile_ended_at`` stamping.

The compile clock must count compilation only: it starts on the first line
that proves the toolchain is building and stops on the summary banner — never
during the dependency download or CMake configure, and never counting an
install's flash phase. Two output shapes have to work: PlatformIO's
``Compiling <path>`` word markers (arduino / esp8266 / libretiny, and esp-idf
via the pio builder), which carry no percentage, and raw esp-idf ninja
``[N/M] Building …`` counters (bluetooth-proxy / idf.py builds), which carry no
``Compiling`` word — the real lines below come from a captured ninja build.
"""

from __future__ import annotations

import pytest

from esphome_device_builder.controllers.firmware.helpers import (
    _parse_progress,
    _stamp_compile_phase,
)
from esphome_device_builder.models.firmware import FirmwareJob, JobType


def _job() -> FirmwareJob:
    return FirmwareJob(job_id="j", configuration="c.yaml", job_type=JobType.COMPILE)


def _feed(job: FirmwareJob, line: str) -> None:
    """Drive one line the way ``_ingest_output_line`` does: parse, then stamp."""
    _stamp_compile_phase(job, line, _parse_progress(line))


class TestPlatformIOWordMarkers:
    """pio prints ``Compiling <path>`` with no percentage — the word starts it."""

    @pytest.mark.parametrize(
        "line",
        [
            "Compiling .pio/build/esp32dev/src/main.cpp.o",
            "Compiling .pioenvs/apy/esp_hw_support/cpu.c.o",
            "Compiling .pio/build/nodemcuv2/core/core_esp8266_main.cpp.o",
            "Compiling .pio/build/bk72xx/src/main.cpp.o",
            "Archiving .pio/build/nodemcuv2/libFrameworkArduino.a",
            "Linking .pio/build/bk72xx/firmware.elf",
            "Building in release mode",
        ],
    )
    def test_word_marker_starts_without_percent(self, line: str) -> None:
        job = _job()
        _feed(job, line)
        assert job.compile_started_at is not None


class TestRawNinjaCounters:
    """esp-idf ninja prints ``[N/M] Building …`` with no ``Compiling`` word."""

    @pytest.mark.parametrize(
        "line",
        [
            # Real captured lines (CR-split, trailing erase-to-eol escape).
            "[1/1547] Generating project_elf_src_esp32s3.c\x1b[K",
            "[6/1547] Building C object esp-idf/esp_adc/adc_cali.c.obj\x1b[K",
            "[995/1547] Building C object esp-idf/bt/bta_hh_utils.c.obj",
            "[1547/1547] Linking CXX executable btp.elf",
        ],
    )
    def test_counter_starts_via_parsed_progress(self, line: str) -> None:
        job = _job()
        _feed(job, line)
        assert job.compile_started_at is not None

    @pytest.mark.parametrize(
        "line",
        [
            # CMake reconfigure + globbed-dir re-check: total under the ninja
            # floor, so no percent parses and the compile hasn't started.
            "[0/2] Re-checking globbed directories...\x1b[K",
            "[1/2] Re-running CMake...\x1b[K",
            "[0/4] Re-checking globbed directories...\x1b[K",
            "[3/97] Performing build step for 'bootloader'",
        ],
    )
    def test_configure_counters_do_not_start(self, line: str) -> None:
        job = _job()
        _feed(job, line)
        assert job.compile_started_at is None


class TestDownloadAndConfigureExcluded:
    @pytest.mark.parametrize(
        "line",
        [
            "Tool Manager: Installing framework-arduinoespressif32",
            "Library Manager: Installing esphome/noise-c @ 0.1.11",
            "Unpacking [####################] 100%",
            "-- Configuring done (3.0s)",
            "-- Building ESP-IDF components for target esp32s3",
            "Executing action: reconfigure",
            "Running ninja in directory /data/build/btp/build",
        ],
    )
    def test_no_start(self, line: str) -> None:
        job = _job()
        _feed(job, line)
        assert job.compile_started_at is None


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
        _feed(job, "Compiling a.cpp.o")
        _feed(job, line)
        assert job.compile_ended_at is not None

    def test_end_ignored_before_start(self) -> None:
        job = _job()
        _feed(job, "[SUCCESS] Took 1.0 seconds")
        assert job.compile_started_at is None
        assert job.compile_ended_at is None


class TestLatching:
    def test_start_latched_once(self) -> None:
        job = _job()
        _feed(job, "Compiling a.cpp.o")
        first = job.compile_started_at
        _feed(job, "[6/1547] Building C object b.c.obj")
        assert job.compile_started_at == first

    def test_end_latched_once(self) -> None:
        job = _job()
        _feed(job, "Compiling a.cpp.o")
        _feed(job, "[SUCCESS] Took 1.0 seconds")
        first = job.compile_ended_at
        _feed(job, "[FAILED] Took 9.0 seconds")
        assert job.compile_ended_at == first

    def test_cleared_by_clear_run_state(self) -> None:
        job = _job()
        _feed(job, "Compiling a.cpp.o")
        _feed(job, "[SUCCESS] Took 1.0 seconds")
        job.clear_run_state()
        assert job.compile_started_at is None
        assert job.compile_ended_at is None


def test_old_job_without_fields_deserializes_to_none() -> None:
    """A job persisted before these fields existed loads with them unset."""
    payload = {
        "job_id": "old",
        "configuration": "c.yaml",
        "job_type": JobType.COMPILE.value,
    }
    job = FirmwareJob.from_dict(payload)
    assert job.compile_started_at is None
    assert job.compile_ended_at is None
