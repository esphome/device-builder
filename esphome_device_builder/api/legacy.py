"""DEPRECATED: Legacy REST + WebSocket endpoints for Home Assistant compatibility.

These endpoints exist only for backward compatibility with the HA ESPHome
integration (via esphome-dashboard-api). They will be removed once HA
migrates to the /ws multiplexed API.

HA uses:
- GET /devices (list configured + importable devices)
- GET /json-config?configuration=... (parsed YAML as JSON)
- /compile (WebSocket, spawn protocol)
- /upload (WebSocket, spawn protocol)

The ``/compile`` and ``/upload`` WebSocket handlers route through the
new firmware-job queue rather than spawning subprocesses directly.
This is what makes HA-triggered builds show up alongside dashboard-
triggered ones in the "Firmware tasks" panel — see issue #394. The
legacy WS frame shape (``{event: "line", data}`` / ``{event: "exit",
code}``) is preserved so unmodified ``esphome-dashboard-api``
clients keep working.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Protocol

import aiohttp
from aiohttp import web
from esphome import yaml_util

from ..helpers.api import CommandError
from ..helpers.event_bus import Event
from ..helpers.json import (
    JSONDecodeError,
    dumps_str,
    dumps_str_non_str_keys,
    json_response,
    loads,
)
from ..models import EventType, JobStatus, JobType

if TYPE_CHECKING:
    from ..helpers.event_bus import EventBus
    from ..models import FirmwareJob


class _LegacyDB(Protocol):
    """Narrow protocol for the bits of ``DeviceBuilder`` this module reads.

    Avoids the circular-import / type-erasure pair the previous
    ``db: object`` + ``# type: ignore[attr-defined]`` shape produced.
    Both ``firmware`` and ``bus`` are typed as the wrapping
    ``Optional`` because ``DeviceBuilder`` initialises them after
    construction (``firmware`` in ``start``, ``bus`` in ``__init__``);
    the production startup order guarantees they're set by the time
    HTTP requests are served, but the legacy handler still defends
    against ``None`` so a future startup-order regression fails
    closed with a controlled exit frame instead of an
    ``AttributeError`` that surfaces to HA as a connection drop.
    """

    bus: EventBus | None
    # Forward-declare the firmware controller as ``object`` since
    # importing it here would re-trigger the circular nudge that
    # motivated the local terminal-event constants below.
    firmware: object | None


_LOGGER = logging.getLogger(__name__)

# Mirrors ``firmware/constants.py``; redefined locally to avoid a
# circular-import nudge between the API layer and the firmware
# controller's private constants module. The set is small and
# stable — three lifecycle events, one terminal status set.
_TERMINAL_EVENT_TYPES = (
    EventType.JOB_COMPLETED,
    EventType.JOB_FAILED,
    EventType.JOB_CANCELLED,
)
_TERMINAL_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED})


async def _stream_job_to_legacy_ws(
    ws: web.WebSocketResponse,
    bus: EventBus,
    job: FirmwareJob,
) -> None:
    """
    Translate a firmware-job's output stream into the legacy WS frame shape.

    The legacy protocol (the only one HA's ``esphome-dashboard-api``
    speaks) expects ``{event: "line", data: <chunk>}`` per stdout
    chunk and ``{event: "exit", code: <int>}`` once the build
    finishes. The new firmware queue exposes those signals via
    bus events (``JOB_OUTPUT`` / ``JOB_COMPLETED`` / ``JOB_FAILED``
    / ``JOB_CANCELLED``) and a buffered ``job.output`` list.

    The race-free pattern:

    1. Snapshot ``job.output`` (sync) — captures every line fired
       so far. The snapshot is a fresh list copy via ``list(...)``
       so a subsequent ``_trim_job_output`` reassign of
       ``job.output`` (``firmware/helpers.py``) doesn't mutate
       what we replay.
    2. ``add_listener`` (sync) — attaches before any further
       events fire. Steps 1 and 2 are sync-adjacent so no
       coroutine yield can interleave a fire between them; the
       runner's loop appends to ``job.output``, optionally trims
       (which reassigns the list — irrelevant to our snapshot
       copy), and fires ``JOB_OUTPUT`` in the same synchronous
       block (``firmware/controller.py:899-918``), so a line can
       either be in the snapshot OR caught by the listener, never
       both and never neither.
    3. Replay snapshot. Lines that arrive during the replay queue
       up via the listener and are drained afterwards.
    4. Drain the queue until a terminal event arrives.
    """
    job_id = job.job_id

    # Pending items are tagged so the drain loop can route line
    # frames vs. the terminal sentinel without a second lookup.
    pending: asyncio.Queue[tuple[str, object]] = asyncio.Queue()

    def _on_event(event: Event) -> None:
        if event.event_type == EventType.JOB_OUTPUT:
            if event.data.get("job_id") == job_id:
                pending.put_nowait(("line", event.data.get("line", "")))
            return
        ev_job = event.data.get("job")
        if ev_job is None or getattr(ev_job, "job_id", None) != job_id:
            return
        # ``exit_code`` is None for cancelled / never-ran jobs;
        # legacy clients want a numeric code so coerce to a
        # generic failure (1) rather than serialising null.
        code = getattr(ev_job, "exit_code", None)
        pending.put_nowait(("exit", code if code is not None else 1))

    snapshot = list(job.output)
    initial_status = job.status
    initial_exit_code = job.exit_code

    with bus.listening((EventType.JOB_OUTPUT, *_TERMINAL_EVENT_TYPES), _on_event):
        for line in snapshot:
            await ws.send_json({"event": "line", "data": line}, dumps=dumps_str)

        # ``compile`` / ``upload`` may resolve a job that's already
        # in a terminal state — most common on a duplicate-submit
        # supersede that lands the previous job in CANCELLED before
        # the new one is created, but also any case where the job
        # transitions during the snapshot above. Send the exit
        # frame and bail.
        if initial_status in _TERMINAL_STATUSES:
            code = initial_exit_code if initial_exit_code is not None else 1
            await ws.send_json({"event": "exit", "code": code}, dumps=dumps_str)
            return

        while True:
            kind, payload = await pending.get()
            if kind == "line":
                await ws.send_json({"event": "line", "data": payload}, dumps=dumps_str)
                continue
            await ws.send_json({"event": "exit", "code": payload}, dumps=dumps_str)
            return


async def _handle_legacy_ws_command(
    request: web.Request,
    job_type: JobType,
) -> web.WebSocketResponse:
    """Route a legacy ``/compile`` or ``/upload`` WS into the firmware queue.

    The legacy spawn protocol still drives the wire shape:

    - ``client → server``: ``{"type": "spawn", "configuration": "kitchen.yaml", "port": "..."}``
    - ``server → client``: ``{"event": "line", "data": "<chunk>"}`` per stdout line
    - ``server → client``: ``{"event": "exit", "code": <int>}`` on completion

    What changed is *how* the build runs: instead of a per-WS
    subprocess that bypasses the dashboard's bookkeeping, the
    request is enqueued through the same ``FirmwareController``
    the new dashboard uses, so the running build appears in the
    "Firmware tasks" panel and survives a page refresh. Closes #394.
    """
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    db: _LegacyDB = request.app["device_builder"]
    firmware = db.firmware
    bus = db.bus
    # Defend against a startup-order regression: production sets
    # both before serving HTTP, but a future refactor that flips
    # that order would otherwise crash with ``AttributeError`` and
    # surface to HA as a connection drop. Failing closed with a
    # ``code: 1`` exit frame keeps the rejection on the legacy
    # protocol's only signalling channel.
    if firmware is None or bus is None:
        _LOGGER.warning(
            "Legacy %s WS rejected: device_builder not fully initialised (firmware=%s, bus=%s)",
            request.path,
            "set" if firmware is not None else "None",
            "set" if bus is not None else "None",
        )
        async for _ in ws:
            await ws.send_json({"event": "exit", "code": 1}, dumps=dumps_str)
            break
        return ws

    async for msg in ws:
        if msg.type != aiohttp.WSMsgType.TEXT:
            break
        try:
            data = loads(msg.data)
        except JSONDecodeError:
            # Legacy clients shouldn't send non-JSON, but if one
            # does we'd rather skip the frame than tear down the
            # whole handler.
            _LOGGER.debug("Ignoring non-JSON frame on %s", request.path)
            continue
        if not isinstance(data, dict) or data.get("type") != "spawn":
            continue

        configuration = data.get("configuration", "")
        port = data.get("port", "") if job_type is JobType.UPLOAD else ""

        try:
            if job_type is JobType.UPLOAD:
                job = await firmware.upload(  # type: ignore[attr-defined]
                    configuration=configuration, port=port
                )
            else:
                job = await firmware.compile(  # type: ignore[attr-defined]
                    configuration=configuration
                )
        except CommandError:
            # Boundary / validation rejection. The legacy spawn
            # protocol uses ``{event: "exit", code}`` as its only
            # signalling channel — any non-zero code reads as
            # "build failed" in HA's UI, matching the prior
            # subprocess shape that surfaced the same rejection
            # via ``code: 1``.
            await ws.send_json({"event": "exit", "code": 1}, dumps=dumps_str)
            break

        await _stream_job_to_legacy_ws(ws, bus, job)
        break

    return ws


def create_legacy_routes() -> web.RouteTableDef:
    """Create backward-compatible REST + WS routes for HA."""
    routes = web.RouteTableDef()

    @routes.get("/devices")
    async def legacy_devices(request: web.Request) -> web.Response:
        """Legacy GET /devices — returns configured + importable devices.

        Calls ``poll`` to refresh the scanner from disk before
        reading. This is the same shape ``DeviceBuilder._run_background``
        uses on its periodic tick — HA's sync-after-edit pattern
        relies on each ``GET /devices`` actually re-walking the
        config directory rather than returning whatever the last
        background tick happened to capture. ``poll`` was named
        ``_request_scan`` before the controller-split refactor;
        the legacy route's call site was missed in the rename and
        crashed with ``AttributeError`` until we caught it via
        issue #376.
        """
        db = request.app["device_builder"]
        devices_ctrl = db.devices
        await devices_ctrl.poll()

        configured = [d.to_dict() for d in devices_ctrl.get_devices()]

        importable = [
            imp.to_dict()
            for name, imp in devices_ctrl.import_result.items()
            if name not in devices_ctrl.ignored_devices
        ]

        return json_response({"configured": configured, "importable": importable})

    @routes.get("/json-config")
    async def legacy_json_config(request: web.Request) -> web.Response:
        """Legacy GET /json-config — parsed YAML config as JSON."""
        configuration = request.query.get("configuration", "")
        db = request.app["device_builder"]
        loop = asyncio.get_running_loop()
        try:
            # ``rel_path`` calls ``Path.resolve``, a blocking syscall —
            # run it in the executor so blockbuster doesn't fault the
            # request on CI.
            config_path = await loop.run_in_executor(None, db.settings.rel_path, configuration)
        except CommandError:
            return json_response({"error": "Forbidden"}, status=403)

        try:
            # ``yaml_util.load_yaml`` expects a ``Path`` (it calls
            # ``fname.open(...)``); a string would raise
            # ``AttributeError: 'str' object has no attribute 'open'``
            # at parse time and the bare ``except`` below would
            # surface it as 500 with that opaque message rather than
            # a real YAML error. Keep the real ``Path`` here.
            config = await loop.run_in_executor(None, yaml_util.load_yaml, config_path)
        except Exception as exc:
            return json_response({"error": str(exc)}, status=500)

        # ESPHome's ``yaml_util.load_yaml`` returns an ``OrderedDict``
        # whose keys are ``EStr`` (a ``str`` subclass that carries
        # source-position info). orjson's strict default rejects
        # non-exact-``str`` keys; ``dumps_str_non_str_keys`` flips
        # the ``OPT_NON_STR_KEYS`` option just for this endpoint.
        return web.json_response(config, dumps=dumps_str_non_str_keys)

    @routes.get("/compile")
    async def legacy_compile(request: web.Request) -> web.WebSocketResponse:
        return await _handle_legacy_ws_command(request, JobType.COMPILE)

    @routes.get("/upload")
    async def legacy_upload(request: web.Request) -> web.WebSocketResponse:
        return await _handle_legacy_ws_command(request, JobType.UPLOAD)

    return routes
