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
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

# 4 KB is a reasonable chunk size — large enough to amortise the
# syscall overhead on a busy pipe, small enough that latency-
# sensitive consumers (live progress bars) see updates quickly.
# The actual pipe buffer is platform-dependent (Linux defaults to
# 64 KB, macOS to 16 KB, Windows varies); we don't need to match
# it because the StreamReader will keep filling on demand.
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


def kill_quietly(proc: asyncio.subprocess.Process) -> None:
    """
    Best-effort ``proc.kill()`` that swallows ``ProcessLookupError``.

    There's a TOCTOU race in every kill site: between a
    ``proc.returncode is None`` check and ``proc.kill()`` firing,
    the child can exit on its own — and ``Process.kill()`` then
    raises ``ProcessLookupError`` because the pid's already
    reaped. Wrap the kill with this helper instead of repeating
    the suppress block at every call site.
    """
    with suppress(ProcessLookupError):
        proc.kill()


@dataclass(frozen=True)
class CapturedSubprocess:
    """Result of :func:`run_subprocess_capture`.

    ``returncode`` is ``None`` only when the subprocess hadn't yet
    been waited at the moment the helper returned — in practice we
    always ``wait`` after a timeout, so ``returncode`` is the
    underlying ``Process.returncode`` once the helper returns.
    ``stdout`` is the captured stdout (which includes stderr when
    the caller passed ``stderr=subprocess.STDOUT``). ``timed_out``
    is ``True`` iff the subprocess didn't finish within the
    *timeout* and we killed it.
    """

    returncode: int | None
    stdout: bytes
    timed_out: bool


async def run_subprocess_capture(
    *args: str,
    timeout: float,
    stdin_data: bytes | None = None,
    merge_stderr: bool = True,
) -> CapturedSubprocess:
    """Spawn *args*, await completion (or *timeout*), capture stdout.

    Shared shape between every "run a command to completion and
    inspect its exit code + output" caller (currently
    :func:`controllers.firmware.helpers._verify_esphome_importable`,
    :func:`helpers.config_bundle.build_yaml_bundle`, and the
    Native-API info worker in
    :class:`controllers._device_state_monitor.api_info.ApiInfoSource`).

    *stdin_data*, when given, is written to the child's stdin (stdin is
    opened as a pipe only then). *merge_stderr* (default) redirects
    stderr onto stdout for a unified stream; pass ``False`` to discard
    stderr so stdout carries only the child's real output (e.g. a clean
    JSON payload).

    Timeout handling: :func:`asyncio.wait_for` raises
    :class:`TimeoutError`; we :func:`kill_quietly` the process,
    await its ``wait()`` so the OS resources release, and return
    a :class:`CapturedSubprocess` with ``timed_out=True``. Caller
    inspects ``timed_out`` rather than handling a raised
    exception, which keeps the common shape "one return, check
    flags" instead of try/except at every call site.

    No retry, no streaming — callers that need either pattern
    use :func:`create_subprocess_exec` directly.

    Cancellation-safe: if the awaiting task is cancelled while
    ``proc.communicate()`` is in flight, ``kill_quietly`` fires
    SIGKILL and the :class:`CancelledError` re-raises
    immediately. The dead subprocess is reaped by asyncio's
    child watcher in the background — we deliberately don't
    ``await proc.wait()`` here because that would either swallow
    the second cancellation (violating
    ``feedback_no_suppress_cancelled_error``) or stall the
    propagation. SIGKILL'd processes exit within microseconds
    and the watcher cleans up regardless.
    """
    proc = await create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(stdin_data), timeout=timeout)
    except TimeoutError:
        kill_quietly(proc)
        await proc.wait()
        return CapturedSubprocess(returncode=proc.returncode, stdout=b"", timed_out=True)
    except asyncio.CancelledError:
        kill_quietly(proc)
        raise
    return CapturedSubprocess(returncode=proc.returncode, stdout=stdout, timed_out=False)


async def iter_lines_with_progress(stream: asyncio.StreamReader) -> AsyncIterator[str]:
    r"""Yield decoded chunks from *stream*, splitting on ``\n`` *or* ``\r``.

    ``StreamReader``'s default ``async for`` iteration only splits
    on ``\n``, which buffers carriage-return-based progress
    output (esptool's ``Writing at 0x... (5%)\r``, PlatformIO's
    progress bars) until the next newline arrives — typically only
    when the operation finishes, so the user sees a long pause and
    then a wall of progress lines instead of a live indicator.

    ``\r\n`` is treated as a *single* logical terminator (one
    chunk ending in ``\r\n``) rather than two — the alternative
    would emit a spurious empty event for every CRLF line on
    Windows where Python's stdout text-mode write translates
    ``\n`` into ``\r\n``.

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

            # Pick the earliest terminator. ``\r\n`` coalesces;
            # a ``\r`` at the very end of the read might be the
            # start of a CRLF whose ``\n`` arrives in the next
            # chunk, so defer until we have more bytes.
            if cr != -1 and (nl == -1 or cr < nl):
                if cr + 1 == len(buf):
                    break  # might be \r\n — wait for the next read
                # ``\r\n`` coalesces; bare ``\r`` is an esptool overwrite.
                end = cr + 2 if buf[cr + 1 : cr + 2] == b"\n" else cr + 1
            else:
                end = nl + 1
            chunk = buf[:end]
            buf = buf[end:]
            yield chunk.decode("utf-8", errors="replace")
