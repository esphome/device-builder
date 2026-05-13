"""Per-connection log streaming + the shared subprocess-stream helper."""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ...helpers.process import kill_quietly
from ...helpers.subprocess import create_subprocess_exec, iter_lines_with_progress
from ...models import StreamEvent

if TYPE_CHECKING:
    from .controller import DevicesController


async def stream_logs(
    controller: DevicesController,
    *,
    configuration: str,
    port: str,
    no_states: bool,
    client: Any,
    message_id: str,
) -> None:
    """Stream live device logs. Per-connection, not queued.

    ``port`` is forwarded to ``esphome logs`` as ``--device`` and
    defaults to ``OTA`` when missing or empty. ``no_states``
    passes ``--no-states`` through so component state-publish
    lines are suppressed at the source; mirrors the legacy
    dashboard's "Show entity state changes" toggle.
    """
    config_path = str(controller._db.settings.rel_path(configuration))
    # Always pass --device. Without one ``esphome logs`` enters an
    # interactive port-choice prompt when multiple targets are
    # visible (serial + OTA); the stdin-less subprocess then
    # crashes with EOFError. (#636)
    resolved_port = port or "OTA"
    # Cache args go before the subcommand; esphome parses
    # --mdns/--dns-address-cache on the top-level parser.
    cache_args = controller.get_ota_address_cache_args(configuration, resolved_port)
    cmd = [
        *controller._esphome_cmd,
        "--dashboard",
        *cache_args,
        "logs",
        config_path,
        "--device",
        resolved_port,
    ]
    if no_states:
        cmd.append("--no-states")
    # Route through the controller's bound delegate so tests that
    # instance-patch ``_stream_subprocess`` still intercept.
    await controller._stream_subprocess(cmd, client, message_id)


def stop_stream(client: Any, stream_id: str) -> dict:
    """Cancel a streaming command on this connection.

    Returns ``{"cancelled": True}`` if a matching in-flight
    stream was found, ``{"cancelled": False}`` otherwise (already
    finished, never registered, or no client context).
    """
    if client is None:
        return {"cancelled": False}
    return {"cancelled": client.cancel_stream(stream_id)}


async def stream_subprocess(
    controller: DevicesController,
    cmd: list[str],
    client: Any,
    message_id: str,
    *,
    line_transform: Callable[[str], str] | None = None,
) -> None:
    """Run a CLI subprocess and stream its merged stdout/stderr to a single client.

    Registers the running task with the client so a peer
    ``devices/stop_stream`` (or a WS disconnect) can cancel it;
    cancellation kills the subprocess so it doesn't keep running
    detached. ``line_transform`` is applied to every output line
    before it leaves the WS handler; ``validate_config`` uses it
    to scrub resolved secrets that ``esphome config`` would
    otherwise leak through the ANSI conceal SGR.
    """
    # Register before the first await so an early ``stop_stream``
    # (during subprocess spawn) still finds and cancels this task.
    task = asyncio.current_task()
    assert task is not None  # always running inside a Task
    client.register_stream(message_id, task)

    env = {**os.environ, "PLATFORMIO_FORCE_ANSI": "true"}
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        assert proc.stdout is not None
        # Use the shared `\n`/`\r` splitter so esptool / PlatformIO
        # carriage-return progress lines surface live instead of
        # buffering until the next newline. Strip the terminator
        # from each event payload; the frontend's logs view appends
        # every event as a new line, unlike the firmware job-output
        # path which preserves terminators for in-place overwrites.
        async for line in iter_lines_with_progress(proc.stdout):
            payload = line.rstrip("\n\r")
            if line_transform is not None:
                payload = line_transform(payload)
            await client.send_event(message_id, StreamEvent.OUTPUT, payload)
        exit_code = await proc.wait()
    except asyncio.CancelledError:
        # Synchronous kill only; no awaits in the cancel path. The
        # finally block reaps the process and ``devices/stop_stream``
        # tells the frontend the cancel succeeded. ``proc`` may be
        # None if cancellation arrived before spawn returned.
        if proc is not None and proc.returncode is None:
            kill_quietly(proc)
        # Honour the cancellation contract; only swallow if no
        # outstanding cancel requests remain on this task.
        if (current := asyncio.current_task()) and current.cancelling():
            raise
        return
    finally:
        client.unregister_stream(message_id)
        if proc is not None and proc.returncode is None:
            # Reap so the transport closes cleanly; shielded so an
            # additional cancellation doesn't strand the subprocess.
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(proc.wait())

    await client.send_event(message_id, "result", {"success": exit_code == 0, "code": exit_code})
