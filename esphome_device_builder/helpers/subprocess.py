"""Subprocess helpers.

Centralises ``asyncio.create_subprocess_exec`` so every spawn forces
``close_fds=False``. Python <3.14's default (``close_fds=True``) makes
the subprocess module ``fork()`` the parent and have the child iterate
``/proc/self/fd`` to close descriptors before ``exec()``; on
memory-pressured systems that copies a non-trivial amount of page
tables for nothing. None of our spawns rely on inherited descriptors
being closed at the boundary, and the upstream esphome dashboard uses
the same pattern in ``esphome.dashboard.util.subprocess``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

# 4 KB matches the typical pipe buffer on Linux/macOS/Windows.
# Larger reads don't help (the kernel rounds down anyway) and
# smaller reads spend more time in the syscall.
_STREAM_READ_SIZE = 4096


async def create_subprocess_exec(
    *args: str,
    **kwargs: Any,
) -> asyncio.subprocess.Process:
    """Spawn a subprocess via ``asyncio.create_subprocess_exec``.

    Positional and keyword arguments are forwarded to the underlying
    call, except ``close_fds`` is always overridden to ``False``.
    Callers must not rely on overriding ``close_fds`` or on kwargs
    that require ``close_fds=True`` (e.g. ``pass_fds``). Use this
    helper everywhere instead of calling
    ``asyncio.create_subprocess_exec`` directly.
    """
    kwargs["close_fds"] = False
    return await asyncio.create_subprocess_exec(*args, **kwargs)


async def iter_lines_with_progress(stream: asyncio.StreamReader) -> AsyncIterator[str]:
    r"""Yield decoded chunks from *stream*, splitting on ``\n`` *or* ``\r``.

    ``StreamReader``'s default ``async for`` iteration only splits
    on ``\n``, which buffers carriage-return-based progress
    output (esptool's ``Writing at 0x... (5%)\r``, PlatformIO's
    progress bars) until the next newline arrives — typically only
    when the operation finishes, so the user sees a long pause and
    then a wall of progress lines instead of a live indicator.

    Each emitted chunk **keeps its trailing terminator** so the
    consumer can decide whether to append a new line or overwrite
    the last one (frontend ansi-log component leans on the
    distinction). Decoding is utf-8 with ``errors="replace"`` so a
    stray byte sequence doesn't kill the stream. Buffer is flushed
    on EOF so a final chunk without a terminator still surfaces.
    """
    buf = b""
    while True:
        data = await stream.read(_STREAM_READ_SIZE)
        if not data:
            if buf:
                yield buf.decode("utf-8", errors="replace")
            return
        buf += data
        while buf:
            nl = buf.find(b"\n")
            cr = buf.find(b"\r")
            if nl == -1 and cr == -1:
                break  # need more bytes before we can split
            if nl == -1:
                idx = cr
            elif cr == -1:
                idx = nl
            else:
                idx = min(nl, cr)
            chunk = buf[: idx + 1]
            buf = buf[idx + 1 :]
            yield chunk.decode("utf-8", errors="replace")
