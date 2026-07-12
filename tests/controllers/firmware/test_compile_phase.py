"""
``compile_started_at`` / ``compile_ended_at`` stamping.

The clock starts on the first build line (PlatformIO ``Compiling`` word
markers with no percentage, or raw esp-idf ninja ``[N/M]`` counters with no
``Compiling`` word — both pinned from captured builds) and stops at the
summary banner; the dependency download and an install's flash never count,
and a stray download/flash percentage never starts it.
"""

from __future__ import annotations

import pytest

from esphome_device_builder.controllers.firmware.helpers import _stamp_compile_phase
from esphome_device_builder.models.firmware import FirmwareJob, JobType


def _job() -> FirmwareJob:
    return FirmwareJob(job_id="j", configuration="c.yaml", job_type=JobType.COMPILE)


class TestPlatformIOWordMarkers:
    """pio prints ``Compiling <path>`` with no percentage — the word starts it."""

    @pytest.mark.parametrize(
        "line",
        [
            # Real esp8266 platformio.log compile lines.
            "Compiling .pioenvs/simple8266/src/esphome/components/api/api_server.cpp.o",
            "Compiling .pio/build/esp32dev/src/main.cpp.o",
            "Compiling .pio/build/bk72xx/src/main.cpp.o",
            "Indexing .pioenvs/simple8266/libFrameworkArduino.a",
            "Linking .pioenvs/simple8266/firmware.elf",
            "Building in release mode",
        ],
    )
    def test_word_marker_starts_without_percent(self, line: str) -> None:
        job = _job()
        _stamp_compile_phase(job, line)
        assert job.compile_started_at is not None

    def test_arduino_bracket_percent_starts(self) -> None:
        job = _job()
        _stamp_compile_phase(job, "[ 17%] Compiling .pio/build/uno/src/main.cpp.o")
        assert job.compile_started_at is not None

    def test_reading_cmake_configuration_starts_esp_idf(self) -> None:
        job = _job()
        _stamp_compile_phase(job, "Reading CMake configuration...")
        assert job.compile_started_at is not None


class TestRawNinjaCounters:
    """esp-idf ninja prints ``[N/M]`` counters; the first one is the build start."""

    @pytest.mark.parametrize(
        "line",
        [
            # Real captured btp_compile.log chunks (CR-split, trailing erase escape).
            "[0/2] Re-checking globbed directories...\x1b[K",
            "[1/2] Re-running CMake...\x1b[K",
            "[1/1547] Generating project_elf_src_esp32s3.c\x1b[K",
            "[6/1547] Building C object esp-idf/esp_adc/adc_cali.c.obj\x1b[K",
            "[3/97] Performing build step for 'bootloader'",
            "[1547/1547] Linking CXX executable btp.elf",
        ],
    )
    def test_any_counter_starts_the_clock(self, line: str) -> None:
        job = _job()
        _stamp_compile_phase(job, line)
        assert job.compile_started_at is not None


class TestStrayPercentDoesNotStart:
    """Download / flash / OTA percentages are not compilation."""

    @pytest.mark.parametrize(
        "line",
        [
            # Real esp8266 platformio.log download bar (percent outside brackets).
            "Unpacking  [------------------------------------]    0%",
            "Library Manager: Installing esphome/noise-c @ 0.1.11",
            # esptool flash progress — parses as a percent, but it's the flash.
            "Writing at 0x00010000... (45 %)",
            "Writing at 0x000cf943 [=>  ]  84.8% 491520/579918 bytes...",
            # ESPHome OTA upload.
            "Uploading: [====      ] 35% ...",
            # Memory-usage report at the end of link.
            "RAM:   [====      ]  37.7% (used 30900 bytes from 81920 bytes)",
            "Flash: [====      ]  41.8% (used 428199 bytes from 1023984 bytes)",
            # A bare parenthesised percent anywhere.
            "Downloading toolchain (45%)",
        ],
    )
    def test_no_start(self, line: str) -> None:
        job = _job()
        _stamp_compile_phase(job, line)
        assert job.compile_started_at is None


class TestDownloadAndNarrationExcluded:
    """Setup narration never starts the clock on its own.

    In a real esp-idf build the clock is already running by the time these
    lines stream ("Reading CMake configuration" starts it, since configure is
    CPU+I/O work of the build); this pins that none of them is a start signal,
    so a pio build's download phase stays out of the count.
    """

    @pytest.mark.parametrize(
        "line",
        [
            "Tool Manager: Installing framework-arduinoespressif32",
            "-- Configuring done (3.0s)",
            "-- Building ESP-IDF components for target esp32s3",
            "Executing action: reconfigure",
            "Running ninja in directory /data/build/btp/build",
            "HARDWARE: ESP8266 80MHz, 80KB RAM, 1MB Flash",
        ],
    )
    def test_no_start(self, line: str) -> None:
        job = _job()
        _stamp_compile_phase(job, line)
        assert job.compile_started_at is None


class TestFullSequences:
    """End-to-end ordering: download first, then the build starts the clock."""

    def test_esp8266_platformio(self) -> None:
        job = _job()
        for line in [
            "Library Manager: Installing esphome/noise-c @ 0.1.11",
            "Unpacking  [------------------------------------]    0%",
            "HARDWARE: ESP8266 80MHz, 80KB RAM, 1MB Flash",
            "Compiling .pioenvs/simple8266/src/esphome/components/api/api_server.cpp.o",
        ]:
            assert job.compile_started_at is None or line.startswith("Compiling")
            _stamp_compile_phase(job, line)
        assert job.compile_started_at is not None

    def test_esp_idf_ninja_download_before_first_counter(self) -> None:
        job = _job()
        # A stray download percent lands before ninja and must not start it.
        _stamp_compile_phase(job, "Downloading esp-idf tool (45%)")
        assert job.compile_started_at is None
        _stamp_compile_phase(job, "[0/2] Re-checking globbed directories...\x1b[K")
        assert job.compile_started_at is not None


class TestCompileEnd:
    @pytest.mark.parametrize(
        "line",
        [
            "===================== [SUCCESS] Took 15.36 seconds =====================",
            "===================== [FAILED] Took 4.10 seconds =====================",
            # Real ANSI banner: colours sit *inside* the brackets.
            "\x1b[0m===== [\x1b[32m\x1b[1mSUCCESS\x1b[0m] Took 14.73 seconds =====\x1b[0m",
            "[\x1b[31m\x1b[1mFAILED\x1b[0m] Took 4.10 seconds",
        ],
    )
    def test_banner_ends_after_start(self, line: str) -> None:
        job = _job()
        _stamp_compile_phase(job, "Compiling a.cpp.o")
        _stamp_compile_phase(job, line)
        assert job.compile_ended_at is not None

    def test_end_ignored_before_start(self) -> None:
        job = _job()
        _stamp_compile_phase(job, "[SUCCESS] Took 1.0 seconds")
        assert job.compile_started_at is None
        assert job.compile_ended_at is None


class TestLatching:
    def test_start_latched_once(self) -> None:
        job = _job()
        _stamp_compile_phase(job, "Compiling a.cpp.o")
        first = job.compile_started_at
        _stamp_compile_phase(job, "[6/1547] Building C object b.c.obj")
        assert job.compile_started_at == first

    def test_end_latched_once(self) -> None:
        job = _job()
        _stamp_compile_phase(job, "Compiling a.cpp.o")
        _stamp_compile_phase(job, "[SUCCESS] Took 1.0 seconds")
        first = job.compile_ended_at
        _stamp_compile_phase(job, "[FAILED] Took 9.0 seconds")
        assert job.compile_ended_at == first

    def test_cleared_by_clear_run_state(self) -> None:
        job = _job()
        _stamp_compile_phase(job, "Compiling a.cpp.o")
        _stamp_compile_phase(job, "[SUCCESS] Took 1.0 seconds")
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
