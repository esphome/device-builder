r"""Progress regex covers real markers, ignores noisy ``%`` lines.

The shipped install / compile pipeline mixes a handful of unrelated
percent-bearing log lines with the few that actually represent build
progress. The ``_parse_progress`` whitelist has to pick out the
real ones (PlatformIO ``[ NN%]`` per-file markers, esptool
``(NN %)``, ESPHome OTA ``Uploading: 100%``) and skip everything
else (PIO platform-extract bars, memory-usage reports, stray
percentages in narrative log text). ESP-IDF builds emit no percent
at all — their ninja ``[N/M]`` counter is parsed separately
(``_parse_ninja_counter``) and ``_ninja_progress`` derives the
percentage, gated on the largest total seen so ``[1/2] Re-running
CMake...`` sub-steps and the bootloader ExternalProject sub-build
never drive the gauge.

The regression these tests guard against: a wide-open
``\d{1,3}%`` regex pinned ``job.progress`` to 100 the moment
``Unpacking [###] 100%`` flew by during a fresh PlatformIO
package install — long before the compile had actually started
real work.
"""

from __future__ import annotations

import pytest

from esphome_device_builder.controllers.firmware.helpers import (
    _ninja_progress,
    _parse_ninja_counter,
    _parse_progress,
)
from esphome_device_builder.models.firmware import FirmwareJob, JobType


def _job() -> FirmwareJob:
    return FirmwareJob(job_id="j1", configuration="kitchen.yaml", job_type=JobType.COMPILE)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("[  1%] Compiling .pio/foo.cpp.o", 1),
        ("[ 17%] Compiling .pio/bar.cpp.o", 17),
        ("[100%] Linking .pioenvs/firmware.elf", 100),
        # leading whitespace is fine, ESPHome's --dashboard mode
        # sometimes prefixes lines with indent.
        ("    [ 42%] Compiling baz.cpp.o", 42),
    ],
)
def test_pio_arduino_compile_marker(line: str, expected: int) -> None:
    assert _parse_progress(line) == expected


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Writing at 0x00010000... (5 %)", 5),
        ("Writing at 0x00050000... (45 %)", 45),
        ("Writing at 0x00100000... (100 %)", 100),
        # esptool sometimes drops the space before %.
        ("Writing at 0x00050000... (45%)", 45),
    ],
)
def test_esptool_writing_marker_legacy(line: str, expected: int) -> None:
    assert _parse_progress(line) == expected


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # Issue #140: newer esptool dropped the parens around the
        # percent and added an ASCII progress bar + bytes counter.
        # The percentage is now decimal — we capture the integer
        # part since the dashboard's progress bar is a coarse 0-100.
        pytest.param(
            "Writing at 0x000cf943 [========================>     ]  84.8% 491520/579918 bytes...",
            84,
            id="decimal_mid",
        ),
        pytest.param(
            "Writing at 0x00010000 [                              ]  0.0% 0/579918 bytes...",
            0,
            id="decimal_zero",
        ),
        pytest.param(
            "Writing at 0x000a1234 [==============================] 100.0% 579918/579918 bytes...",
            100,
            id="decimal_full",
        ),
        # Carriage-return-terminated chunk: ``iter_lines_with_progress``
        # splits on ``\r`` so esptool's in-place line refreshes survive
        # the pipe. The trailing ``\r`` is part of the chunk handed to
        # ``_parse_progress``; it must not break the match.
        pytest.param(
            "Writing at 0x000cf943 [=>     ]  42.5% 200000/579918 bytes...\r",
            42,
            id="cr_terminated",
        ),
        # ANSI-prefixed line — the runner sets ``FORCE_COLOR=1`` /
        # ``CLICOLOR_FORCE=1`` so esptool emits ``\x1b[2K`` clear-line
        # escapes before each refresh. An anchored ``^\s*`` regex
        # would silently fail in production (the escapes aren't
        # whitespace) while passing in plain-text tests. Pinning
        # this case prevents a future "tighten the regex" refactor
        # from re-introducing the anchor and breaking serial install
        # progress capture again — that's exactly the regression
        # this PR fixes (issue #140).
        pytest.param(
            "\x1b[2KWriting at 0x000cf943 [=>     ]  42.5% 200000/579918 bytes...",
            42,
            id="ansi_prefixed",
        ),
    ],
)
def test_esptool_writing_marker_new(line: str, expected: int) -> None:
    assert _parse_progress(line) == expected


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Uploading: [====================] 100% Done...", 100),
        ("Uploading: [====      ] 35% ...", 35),
        ("    Uploading: [===========] 50%", 50),
    ],
)
def test_esphome_ota_marker(line: str, expected: int) -> None:
    assert _parse_progress(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        # The original bug report: PIO platform-package extract bar
        # pinned the dashboard to 100% before the compile had
        # actually started any meaningful work.
        "Unpacking [####################################] 100%",
        "Unpacking  [##                                  ] 5%",
        # PIO emits a memory-usage report at the end of the link
        # step. The percentages there describe how full each
        # memory region is, not build progress.
        "RAM:   [==        ]  19.3% (used 63276 bytes from 327680 bytes)",
        "Flash: [========  ]  80.0% (used 1467116 bytes from 1835008 bytes)",
        # Narrative log text that happens to mention a percentage
        # — should never be treated as progress.
        "Saved 75% of build artifacts to cache.",
        "Coverage report: lines 87% / branches 65%",
        "INFO Flash erased — 100% complete in 0.5s",
        # No percentage at all.
        "INFO Successfully compiled program.",
        "Compiling .pio/foo.cpp.o",
        "",
    ],
)
def test_noisy_lines_ignored(line: str) -> None:
    assert _parse_progress(line) is None


def test_over_one_hundred_is_dropped() -> None:
    # Hypothetical malformed esptool output. ``101`` would fall
    # outside our 0..100 contract and pollute the UI.
    assert _parse_progress("Writing at 0x00010000... (101 %)") is None


def test_three_digit_within_range() -> None:
    # 100 is allowed; the regex accepts up to three digits to
    # cover this exact value.
    assert _parse_progress("[100%] Linking firmware.elf") == 100


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            "[907/1424] Building C object esp-idf/bt/CMakeFiles/__idf_bt.dir/"
            "host/bluedroid/bta/av/bta_av_cfg.c.obj",
            (907, 1424),
        ),
        ("[1424/1424] Linking .pioenvs/firmware.elf", (1424, 1424)),
        ("[1/1424] Generating memory view", (1, 1424)),
        ("[1/2] Re-running CMake...", (1, 2)),
        ("    [907/1424] Building C object foo.c.obj", (907, 1424)),
        # CR-terminated in-place refresh chunk.
        ("[907/1424] Building C object foo.c.obj\r", (907, 1424)),
        # ANSI clear-line prefix — a bare ``^\s*`` anchor would
        # silently fail in production while passing plain-text tests
        # (same trap as the esptool ``Writing at`` pattern).
        ("\x1b[2K[907/1424] Building C object foo.c.obj", (907, 1424)),
    ],
)
def test_ninja_counter_lines_parse(line: str, expected: tuple[int, int]) -> None:
    assert _parse_ninja_counter(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        # Malformed: done past total.
        "[150/100] Building C object foo.c.obj",
        # Mid-line counters are narrative text, not progress.
        "note: see [110/200] above",
        # Ninja always puts a space after the ``]`` — a bare or
        # glued counter isn't its output shape.
        "[907/1424]",
        "[907/1424]Building C object foo.c.obj",
        # Build noise that brackets digits without being a counter.
        "otadata,data,ota,0x9000,8K,",
        "*" * 79,
    ],
)
def test_non_counter_lines_ignored(line: str) -> None:
    assert _parse_ninja_counter(line) is None


def test_parse_progress_leaves_counters_alone() -> None:
    assert _parse_progress("[907/1424] Building C object foo.c.obj") is None


@pytest.mark.parametrize(
    "steps",
    [
        # The bootloader ExternalProject sub-build (123 steps on IDF 5.x)
        # streams its own counter mid-run; its [N/N] final step must not
        # pin the gauge to 100 while the app build is at 99.
        pytest.param(
            [
                ("[10/1133] Building C object a.c.obj", 0),
                ("[123/123] Linking CXX executable bootloader.elf", None),
                ("[1123/1133] Completed 'bootloader'", 99),
                ("[1133/1133] Linking .pioenvs/firmware.elf", 100),
            ],
            id="subbuild_final_counter_does_not_latch_100",
        ),
        pytest.param(
            [
                ("[1/2] Re-running CMake...", None),
                ("[97/97] Linking bootloader.elf", None),
            ],
            id="totals_under_floor_never_start_the_gauge",
        ),
        # Ninja recomputes its total as edges are discovered; an equal or
        # larger total stays authoritative.
        pytest.param(
            [
                ("[10/100] Building C object a.c.obj", 10),
                ("[60/120] Building C object b.c.obj", 50),
            ],
            id="growing_total_keeps_advancing",
        ),
    ],
)
def test_ninja_gauge_gating(steps: list[tuple[str, int | None]]) -> None:
    job = _job()
    for line, expected in steps:
        assert _ninja_progress(job, line) == expected


def test_ninja_total_stays_off_the_wire() -> None:
    job = _job()
    assert _ninja_progress(job, "[500/1133] Building C object a.c.obj") == 44
    assert job.ninja_total == 1133
    assert "ninja_total" not in job.to_dict()
    assert FirmwareJob.from_dict(job.to_dict()).ninja_total == 0


def test_clear_run_state_resets_ninja_total() -> None:
    # A mid-build re-route reuses the same job instance; a stale total
    # from the aborted run must not gate the fresh run's counters.
    job = _job()
    _ninja_progress(job, "[500/1133] Building C object a.c.obj")
    job.clear_run_state()
    assert job.ninja_total == 0
    assert _ninja_progress(job, "[60/120] Building C object b.c.obj") == 50
