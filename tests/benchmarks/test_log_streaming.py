r"""Benchmarks for the log-streaming hot path.

``iter_lines_with_progress`` runs on every chunk of subprocess
output for both the firmware-job log path
(``controllers/firmware.py``) and the WebSocket logs/validate path
(``controllers/devices.py:_stream_subprocess``). A regression in
the splitter shows up as visible UI lag during a flash — esptool
emits hundreds of ``\r``-terminated progress lines per second on
a fast LAN, and every one of them goes through this loop.

CodSpeed runs these in CI so a benchmark delta against ``main``
flags performance regressions before they land. Mirrors the
``aioesphomeapi`` / ``habluetooth`` pattern.

Two benchmarks total:

- ``test_iter_lines_with_progress_summary`` — parametrised across
  the four streaming shapes the splitter has to handle (pure
  ``\n``, pure ``\r``, ``\r\n``, mixed). One row per shape in the
  CodSpeed report, easy to read which input pattern regressed.
- ``test_iter_lines_with_progress_split_across_reads`` — feeds
  the stream in 64-byte chunks so lines (and CRLF terminators in
  particular) straddle read boundaries. Exercises the partial-
  buffer + CRLF-deferral path that's hard to trigger from the
  one-shot ``feed_data`` benchmarks above.
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


@pytest.mark.parametrize(
    ("payload", "expected_count"),
    [
        # Pure \n: compile-output baseline (most of a successful compile
        # is plain newline-terminated lines).
        pytest.param(_NEWLINE_PAYLOAD, 1000, id="newline_1k"),
        # Pure \r: esptool's progress writes during the flash phase.
        # Exercises the bare-CR-vs-CRLF lookahead on every byte.
        pytest.param(_CR_PROGRESS_PAYLOAD, 1000, id="cr_progress_1k"),
        # CRLF: Windows / PlatformIO output. Exercises the
        # CRLF-coalesce path; on Windows the kernel hands us this
        # shape because text-mode stdout translates \n → \r\n.
        pytest.param(_CRLF_PAYLOAD, 1000, id="crlf_1k"),
        # Mixed: closest to the production shape during a real flash.
        pytest.param(_MIXED_PAYLOAD, 1000, id="mixed_1k"),
    ],
)
def test_iter_lines_with_progress_summary(
    benchmark: BenchmarkFixture,
    payload: bytes,
    expected_count: int,
) -> None:
    """Run the splitter against each of the four streaming shapes.

    One CodSpeed row per shape so a regression report shows which
    input pattern moved without scattering the signal across
    separately-named benchmarks.
    """

    @benchmark
    def run() -> None:
        chunks = _drive(payload)
        assert chunks == expected_count


def test_iter_lines_with_progress_split_across_reads(
    benchmark: BenchmarkFixture,
) -> None:
    r"""Lines straddling read-buffer boundaries — the partial-buffer + CRLF-deferral path.

    The kernel hands us 4 KB chunks; lines longer than that
    arrive as multiple reads that the splitter has to buffer
    until a terminator shows up. ``\r\n`` straddling the boundary
    additionally exercises the CRLF-deferral logic — when ``\r``
    lands at the end of a chunk we have to wait for the next read
    to decide whether it's bare-CR or part of CRLF, otherwise the
    pair would split into two events.

    Use the CRLF payload (not pure ``\n``) so the deferral path
    actually runs; feed the stream in 64-byte chunks to guarantee
    plenty of partial-buffer hits — at 1000 ``\r\n``-terminated
    lines averaging ~25 bytes each, every CRLF has a non-trivial
    chance of landing on a chunk boundary.
    """
    payload = _CRLF_PAYLOAD

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
