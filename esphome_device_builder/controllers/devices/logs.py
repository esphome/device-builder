"""Per-connection log streaming + the shared subprocess-stream helper."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ...helpers.api import CommandError, registered_stream
from ...helpers.process import kill_subtree_quietly
from ...helpers.subprocess import create_subprocess_exec, iter_lines_with_progress
from ...helpers.windows_job_object import WindowsJobObject
from ...models import OTA_PORT, ErrorCode, StreamEvent

_LOGGER = logging.getLogger(__name__)

# How long a killed child may take to exit before the slot is given up on it.
_REAP_TIMEOUT = 5.0

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
    """
    Stream live device logs. Per-connection, not queued.

    Defaults ``port`` to ``OTA`` when missing or empty;
    ``no_states`` passes ``--no-states`` through to suppress
    component state-publish lines at the source.
    """
    config_path = str(controller._db.settings.rel_path(configuration))
    # Always pass --device; without one ``esphome logs`` enters an
    # interactive port-choice prompt that crashes the stdin-less
    # subprocess with EOFError. (#636)
    resolved_port = port or OTA_PORT
    # Cache args go before the subcommand; esphome parses
    # --mdns/--dns-address-cache on the top-level parser.
    cache_args = controller.get_ota_address_cache_args(configuration, resolved_port)
    cmd = [
        *controller.state.esphome_cmd,
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
    """
    Cancel a streaming command on this connection.

    Returns ``{"cancelled": True}`` if a matching in-flight
    stream was found, ``{"cancelled": False}`` otherwise.
    """
    if client is None:
        return {"cancelled": False}
    return {"cancelled": client.cancel_stream(stream_id)}


async def stream_subprocess(
    cmd: list[str],
    client: Any,
    message_id: str,
    *,
    line_transform: Callable[[str], str] | None = None,
    slot: asyncio.Semaphore | None = None,
    slot_timeout: float | None = None,
    idle_timeout: float | None = None,
) -> None:
    """
    Run a CLI subprocess and stream its merged stdout/stderr to a single client.

    Registers the running task with the client so a peer
    ``devices/stop_stream`` (or a WS disconnect) can cancel it
    and kill the subprocess. ``line_transform`` is applied per
    line before it leaves the WS handler. With *slot*, the run
    waits up to *slot_timeout* for a permit and holds it to the
    end; a run silent for *idle_timeout* is killed. Either bound
    answers ``UNAVAILABLE``.
    """
    # ``registered_stream`` enters before the first await so an early
    # ``stop_stream`` (during the slot wait or subprocess spawn) still
    # finds and cancels this task.
    with registered_stream(client, message_id):
        if slot is not None:
            if slot.locked():
                await client.send_event(message_id, StreamEvent.OUTPUT, "Waiting for a free slot…")
            try:
                async with asyncio.timeout(slot_timeout):
                    await slot.acquire()
            except TimeoutError as err:
                _LOGGER.warning(
                    "Stream %s refused, no slot freed within %ss: %s",
                    message_id,
                    slot_timeout,
                    " ".join(cmd),
                )
                msg = "Too many concurrent runs; retry shortly"
                raise CommandError(ErrorCode.UNAVAILABLE, msg) from err
        try:
            async with asyncio.timeout(idle_timeout) as deadline:
                exit_code = await _run_streaming(
                    cmd, client, message_id, line_transform, deadline, idle_timeout
                )
        except TimeoutError as err:
            if not deadline.expired():
                raise
            _LOGGER.warning(
                "Stream %s stopped, silent for %ss: %s", message_id, idle_timeout, " ".join(cmd)
            )
            msg = f"No output for {idle_timeout:.0f}s; the run was stopped"
            raise CommandError(ErrorCode.UNAVAILABLE, msg) from err
        finally:
            if slot is not None:
                slot.release()
        if exit_code is None:
            # Swallowed cancel: the stop_stream reply is the client's terminal signal.
            return
        await client.send_event(
            message_id, "result", {"success": exit_code == 0, "code": exit_code}
        )


async def _run_streaming(
    cmd: list[str],
    client: Any,
    message_id: str,
    line_transform: Callable[[str], str] | None,
    deadline: asyncio.Timeout,
    idle_timeout: float | None,
) -> int | None:
    """Spawn *cmd* and forward its lines; ``None`` when a swallowed cancel ended the run."""
    env = {**os.environ, "PLATFORMIO_FORCE_ANSI": "true"}
    loop = asyncio.get_running_loop()
    proc: asyncio.subprocess.Process | None = None
    win_job: WindowsJobObject | None = None
    try:
        proc = await create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
        win_job = WindowsJobObject.create_for_pid(proc.pid)
        assert proc.stdout is not None
        # Use the shared `\n`/`\r` splitter so esptool / PlatformIO
        # carriage-return progress lines surface live; strip the
        # terminator since the frontend's logs view appends every
        # event as a new line.
        async for line in iter_lines_with_progress(proc.stdout):
            if idle_timeout is not None:
                _extend_idle_deadline(deadline, loop.time(), idle_timeout)
            payload = line.rstrip("\n\r")
            if line_transform is not None:
                payload = line_transform(payload)
            await client.send_event(message_id, StreamEvent.OUTPUT, payload)
        return await proc.wait()
    except asyncio.CancelledError:
        # Honour the asyncio cancellation contract: only swallow
        # if no outstanding cancel requests remain (asyncio.timeout
        # / TaskGroup may have called Task.uncancel()).
        if (current := asyncio.current_task()) and current.cancelling():
            raise
        return None
    except Exception:
        if proc is not None and proc.returncode is None:
            _LOGGER.warning("Stream %s failed with its child alive; killing it", message_id)
        raise
    finally:
        try:
            if proc is not None and proc.returncode is None:
                # Synchronous kill before the only await; the shield keeps the
                # reap running while a cancellation landing here propagates.
                kill_subtree_quietly(proc, win_job=win_job)
                await _reap(proc, message_id)
        finally:
            if win_job is not None:
                win_job.close()


async def _reap(proc: asyncio.subprocess.Process, message_id: str) -> None:
    """Wait for a killed *proc* to exit; warn and move on after ``_REAP_TIMEOUT``."""
    try:
        await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=_REAP_TIMEOUT)
    except TimeoutError:
        _LOGGER.warning(
            "Stream %s child %d did not exit %ss after the kill",
            message_id,
            proc.pid,
            _REAP_TIMEOUT,
        )


def _extend_idle_deadline(deadline: asyncio.Timeout, now: float, idle_timeout: float) -> None:
    """Push *deadline* out to ``now + idle_timeout``, skipping reschedules closer than a second."""
    when = deadline.when()
    if when is None or when - now < idle_timeout - min(1.0, idle_timeout / 2):
        deadline.reschedule(now + idle_timeout)
