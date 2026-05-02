r"""Benchmarks for the log-streaming hot path.

``iter_lines_with_progress`` runs on every chunk of subprocess
output for both the firmware-job log path
(``controllers/firmware.py``) and the WebSocket logs/validate path
(``controllers/devices.py:_stream_subprocess``). A regression in
the splitter shows up as visible UI lag during a flash — esptool
emits hundreds of ``\r``-terminated progress lines per second on
a fast LAN, and every one of them goes through this loop.

CodSpeed runs these under instrumentation in CI so a benchmark
delta against ``main`` flags performance regressions before they
land. Mirrors the ``aioesphomeapi`` / ``habluetooth`` pattern.
"""

from __future__ import annotations

import asyncio

import pytest
from pytest_codspeed import BenchmarkFixture

from esphome_device_builder.helpers.subprocess import iter_lines_with_progress


def _drive(payload: bytes) -> int:
    """Run the splitter to completion against *payload* and return the chunk count.

    Construct the ``StreamReader`` inside the coroutine so it
    binds to ``asyncio.run``'s loop — building it at module load
    time would crash on the second benchmark with
    ``RuntimeError: no current event loop`` once the prior
    ``asyncio.run`` cleared the thread-local. Returning a value
    the benchmark can keep around stops the loop from being
    optimised away and lets us assert the chunk count.
    """

    async def _consume() -> int:
        reader = asyncio.StreamReader()
        reader.feed_data(payload)
        reader.feed_eof()
        count = 0
        async for _chunk in iter_lines_with_progress(reader):
            count += 1
        return count

    return asyncio.run(_consume())


_NEWLINE_PAYLOAD = b"".join(
    f"compile output line {i:04d} with some realistic length\n".encode() for i in range(1000)
)
_CR_PROGRESS_PAYLOAD = b"".join(
    f"Writing at 0x{i:08x}... ({i % 100:>3}%)\r".encode() for i in range(1000)
)
_CRLF_PAYLOAD = b"".join(f"PlatformIO output line {i:04d}\r\n".encode() for i in range(1000))
_MIXED_PAYLOAD = b"".join(
    [
        # ~70% newline-terminated compile output, ~30% \r progress —
        # roughly the shape of a real ``esphome run`` mid-flash.
        *(f"line {i}\n".encode() if i % 10 < 7 else f"progress {i}\r".encode() for i in range(1000))
    ]
)


def test_iter_lines_with_progress_newline_only(benchmark: BenchmarkFixture) -> None:
    r"""Pure ``\n``-delimited stream — the compile-output baseline.

    Most of a successful compile is plain newline-terminated lines;
    this is the steady-state shape the splitter has to handle
    without overhead from the `\r` lookahead path.
    """

    @benchmark
    def run() -> None:
        chunks = _drive(_NEWLINE_PAYLOAD)
        assert chunks == 1000


def test_iter_lines_with_progress_carriage_return(benchmark: BenchmarkFixture) -> None:
    r"""Pure ``\r`` progress stream — esptool writing flash blocks.

    esptool emits progress in this shape during the upload phase;
    each chunk surfaces as its own log event so the user sees a
    live percentage. The splitter's `\r`-lookahead path has to
    decide bare-CR vs CRLF on every byte the kernel hands us.
    """

    @benchmark
    def run() -> None:
        chunks = _drive(_CR_PROGRESS_PAYLOAD)
        assert chunks == 1000


def test_iter_lines_with_progress_crlf(benchmark: BenchmarkFixture) -> None:
    r"""Pure ``\r\n``-terminated stream — Windows / PlatformIO output.

    ``\r\n`` coalesces into a single chunk per logical line. The
    coalesce path is hot on Windows where Python's text-mode stdout
    translates ``\n`` into ``\r\n`` on the wire.
    """

    @benchmark
    def run() -> None:
        chunks = _drive(_CRLF_PAYLOAD)
        assert chunks == 1000


def test_iter_lines_with_progress_mixed(benchmark: BenchmarkFixture) -> None:
    r"""Realistic mid-flash mix of compile lines and progress chunks.

    Closest to the production shape — ``esphome run`` writes
    PlatformIO compile output (newline-terminated) mixed with
    esptool's progress lines (``\r``-terminated) once it's
    flashing. Tracks the all-up cost.
    """

    @benchmark
    def run() -> None:
        chunks = _drive(_MIXED_PAYLOAD)
        assert chunks == 1000


def test_iter_lines_with_progress_split_across_reads(
    benchmark: BenchmarkFixture,
) -> None:
    """Lines straddling read-buffer boundaries — the partial-buffer path.

    The kernel hands us 4 KB chunks; lines longer than that arrive
    as multiple reads that the splitter has to buffer until a
    terminator shows up. CRLF straddling the boundary additionally
    exercises the deferral logic. This benchmark feeds the stream
    in 64-byte chunks to guarantee plenty of partial-buffer hits.
    """
    payload = _NEWLINE_PAYLOAD  # 1000 newline-terminated lines

    async def _consume_split() -> int:
        reader = asyncio.StreamReader()
        for offset in range(0, len(payload), 64):
            reader.feed_data(payload[offset : offset + 64])
        reader.feed_eof()
        count = 0
        async for _chunk in iter_lines_with_progress(reader):
            count += 1
        return count

    @benchmark
    def run() -> None:
        chunks = asyncio.run(_consume_split())
        assert chunks == 1000


@pytest.mark.parametrize(
    "label,payload,expected_count",
    [
        ("newline_1k", _NEWLINE_PAYLOAD, 1000),
        ("cr_progress_1k", _CR_PROGRESS_PAYLOAD, 1000),
        ("crlf_1k", _CRLF_PAYLOAD, 1000),
        ("mixed_1k", _MIXED_PAYLOAD, 1000),
    ],
)
def test_iter_lines_with_progress_summary(
    benchmark: BenchmarkFixture,
    label: str,
    payload: bytes,
    expected_count: int,
) -> None:
    """Parametrised summary across all four streaming shapes.

    Gives CodSpeed a single comparable line per shape so the
    instrumentation report shows which input pattern regressed (if
    any) rather than burying the signal in four separately-named
    benchmarks.
    """

    @benchmark
    def run() -> None:
        chunks = _drive(payload)
        assert chunks == expected_count
