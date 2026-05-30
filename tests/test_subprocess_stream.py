r"""Tests for ``iter_lines_with_progress``.

The helper splits subprocess stdout on ``\n`` *or* ``\r`` so esptool's
``Writing at 0x... (5%)\r`` progress lines and PlatformIO's progress
bars surface live instead of buffering until the next newline.
``StreamReader``'s default ``async for`` only splits on ``\n``, which
is the cause of the original "wall of progress lines after a long
pause" symptom in `devices/logs`.

Each yielded chunk keeps its trailing terminator so the consumer can
distinguish "new line" (``\n``) from "overwrite the previous line"
(``\r``). Decoding is utf-8 with ``errors="replace"`` so a stray
byte sequence doesn't kill the stream.
"""

from __future__ import annotations

import asyncio

from esphome_device_builder.helpers.subprocess import iter_lines_with_progress


def _stream(data: bytes) -> asyncio.StreamReader:
    r"""Build a StreamReader pre-loaded with *data* and EOF."""
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


async def _collect(stream: asyncio.StreamReader) -> list[str]:
    return [chunk async for chunk in iter_lines_with_progress(stream)]


async def test_splits_on_newline() -> None:
    r"""The traditional `\n`-delimited case still works.

    The helper has to be a strict superset of ``StreamReader``'s
    default iteration; any ``\n``-only consumer that switches to
    it must keep seeing the same chunks.
    """
    chunks = await _collect(_stream(b"alpha\nbeta\ngamma\n"))
    assert chunks == ["alpha\n", "beta\n", "gamma\n"]


async def test_splits_on_carriage_return() -> None:
    r"""`\\r` flushes too — esptool's progress lines surface live.

    Pre-fix, ``Writing at 0x... (5%)\\r`` would sit in the
    StreamReader's buffer until the operation finished and a final
    ``\\n`` arrived, producing a multi-screen wall of progress
    output instead of a live indicator.
    """
    chunks = await _collect(_stream(b"5%\r25%\r50%\r"))
    assert chunks == ["5%\r", "25%\r", "50%\r"]


async def test_crlf_coalesces_to_single_chunk() -> None:
    r"""`\r\n` is one logical line ending, not two events.

    Python's text-mode stdout on Windows translates ``\n`` into
    ``\r\n`` on write — splitting CRLF into ``"foo\r"`` and
    ``"\n"`` would emit a spurious empty event for every
    Windows-emitted line once a downstream consumer strips the
    terminator. Coalescing to one ``"foo\r\n"`` chunk keeps the
    event count matching the *logical* line count regardless of
    platform.
    """
    chunks = await _collect(_stream(b"foo\r\nbar\n"))
    assert chunks == ["foo\r\n", "bar\n"]


async def test_bare_cr_followed_by_data_is_overwrite() -> None:
    r"""A bare ``\r`` (no ``\n``) is the esptool-style overwrite case.

    Distinct from CRLF: ``"5%\r10%\r"`` should produce two
    progress chunks (``"5%\r"``, ``"10%\r"``), not get coalesced
    into a single one. The lookahead for ``\n`` only triggers
    when ``\n`` actually follows.
    """
    chunks = await _collect(_stream(b"5%\r10%\r"))
    assert chunks == ["5%\r", "10%\r"]


async def test_cr_at_end_of_read_defers_until_next_chunk() -> None:
    r"""A ``\r`` at the read boundary waits for the next byte.

    Without the lookahead deferral, a ``\r`` that turns out to be
    the start of ``\r\n`` (when the ``\n`` arrives in a later
    read) would surface as a bare-``\r`` overwrite chunk plus a
    standalone ``\n`` chunk — the exact split we're trying to
    avoid. Deferring keeps CRLF coalescing correct even when the
    pipe boundary cuts a line ending in half.
    """
    reader = asyncio.StreamReader()
    reader.feed_data(b"foo\r")  # ends mid-CRLF
    reader.feed_data(b"\nbar\n")
    reader.feed_eof()

    chunks = await _collect(reader)
    assert chunks == ["foo\r\n", "bar\n"]


async def test_eof_flushes_trailing_buffer() -> None:
    """A final chunk without a terminator still surfaces.

    Without the EOF flush, ``echo -n "no newline"`` would silently
    swallow the final line — the ``buf`` would never hit a
    delimiter and the function would return without yielding it.
    """
    chunks = await _collect(_stream(b"clean\nincomplete"))
    assert chunks == ["clean\n", "incomplete"]


async def test_handles_invalid_utf8_with_replace() -> None:
    """Bad bytes don't kill the stream — decode with `errors='replace'`.

    Subprocess output is occasionally polluted by raw escape codes
    or bytes from a non-UTF-8 locale. Crashing on a stray byte
    would silently truncate the user's logs at the offending
    character; ``errors="replace"`` substitutes U+FFFD instead.
    """
    chunks = await _collect(_stream(b"good\n\xff\xfe bad\n"))
    assert len(chunks) == 2
    assert chunks[0] == "good\n"
    assert chunks[1].endswith(" bad\n")
    assert "�" in chunks[1]  # replacement marker present


async def test_empty_stream_yields_nothing() -> None:
    """Closing without writing anything is a no-op (no spurious empty chunk)."""
    chunks = await _collect(_stream(b""))
    assert chunks == []


async def test_chunks_split_across_multiple_reads() -> None:
    """A line straddling two reads still emits as a single chunk.

    The internal buffer accumulates partial bytes until a
    terminator arrives. Without the buffer, a ``read(4096)`` that
    cuts a line in half would emit two truncated chunks.
    """
    reader = asyncio.StreamReader()
    # First chunk has no terminator.
    reader.feed_data(b"long line that arrives in two")
    # Second chunk completes the line.
    reader.feed_data(b" pieces\n")
    reader.feed_eof()

    chunks = await _collect(reader)
    assert chunks == ["long line that arrives in two pieces\n"]


async def test_realistic_esptool_progress_output() -> None:
    r"""End-to-end shape of an esptool progress stream.

    The pre-fix bug for `devices/logs` was specifically about
    esptool — its progress lines use ``\\r`` for in-place updates
    and end with ``\\n`` only when a stage completes. A consumer
    using the default ``async for`` saw a long pause then a wall
    of intermediate progress steps; the helper makes each `\\r`
    chunk surface as it's read.
    """
    payload = (
        b"Writing at 0x00010000... (5%)\r"
        b"Writing at 0x00010000... (50%)\r"
        b"Writing at 0x00010000... (100%)\r"
        b"Wrote 524288 bytes\n"
    )
    chunks = await _collect(_stream(payload))
    assert chunks == [
        "Writing at 0x00010000... (5%)\r",
        "Writing at 0x00010000... (50%)\r",
        "Writing at 0x00010000... (100%)\r",
        "Wrote 524288 bytes\n",
    ]
