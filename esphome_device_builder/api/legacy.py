"""DEPRECATED: Legacy REST + WebSocket endpoints for Home Assistant compatibility.

These endpoints exist only for backward compatibility with the HA ESPHome
integration (via esphome-dashboard-api). They will be removed once HA
migrates to the /ws multiplexed API.

HA uses:
- GET /devices (list configured + importable devices)
- GET /json-config?configuration=... (parsed YAML as JSON)
- /compile (WebSocket, spawn protocol)
- /upload (WebSocket, spawn protocol)

GET /ping (``{<config>.yaml: True|False|None}`` online-status map) is
consumed by third-party widgets (gethomepage/homepage) rather than HA
core; it is restored here for backward compatibility with the legacy
dashboard.

The ``/compile`` and ``/upload`` WebSocket handlers route through the
new firmware-job queue rather than spawning subprocesses directly.
This is what makes HA-triggered builds show up alongside dashboard-
triggered ones in the "Firmware tasks" panel — see issue #394. The
legacy WS frame shape (``{event: "line", data}`` / ``{event: "exit",
code}``) is preserved so unmodified ``esphome-dashboard-api``
clients keep working.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import web

from ..helpers.api import CommandError
from ..helpers.async_ import run_in_executor
from ..helpers.device_yaml import EsphomeConfigUnavailableError, run_esphome_config
from ..helpers.event_bus import Event, StreamControls, stream_events
from ..helpers.json import (
    JSONDecodeError,
    dumps_str,
    json_response,
    loads,
)
from ..models import (
    TERMINAL_JOB_EVENTS,
    TERMINAL_JOB_STATUSES,
    DeviceState,
    EventType,
    FirmwareJob,
    JobType,
)

if TYPE_CHECKING:
    from ..device_builder import DeviceBuilder
    from ..helpers.event_bus import EventBus


_LOGGER = logging.getLogger(__name__)


def _line_frame(line: str) -> dict[str, Any]:
    """Build a legacy ``{event: "line", data: <chunk>}`` frame."""
    return {"event": "line", "data": line}


def _exit_frame(exit_code: int | None) -> dict[str, Any]:
    """Build a legacy ``{event: "exit", code: <int>}`` frame.

    ``exit_code`` is ``None`` for cancelled / never-ran jobs;
    legacy clients want a numeric code so coerce to a generic
    failure (1) rather than serialising null.
    """
    return {"event": "exit", "code": exit_code if exit_code is not None else 1}


# Legacy ``/ping`` tri-state; ``.get`` defaults UNKNOWN (and any future state) to None.
_STATE_TO_BOOL: dict[DeviceState, bool] = {
    DeviceState.ONLINE: True,
    DeviceState.OFFLINE: False,
}


class _LegacyWSWriter:
    """``stream_events`` client adapter that writes pre-built frames.

    ``stream_events``'s contract is ``send_event(message_id, name,
    payload)`` per drained item; the legacy protocol has neither
    addressing nor routing, so the adapter ignores the first two
    args and forwards *payload* (a fully-formed legacy frame dict)
    to ``ws.send_json``. Building the frame at the producer side
    (in ``handle_event`` / ``send_initial``) keeps the adapter
    one line and removes the stringly-typed name switch the
    earlier version had.
    """

    def __init__(self, ws: web.WebSocketResponse) -> None:
        self._ws = ws

    async def send_event(self, _message_id: str, _name: str, payload: Any) -> None:
        await self._ws.send_json(payload, dumps=dumps_str)


# Stream-event ``name`` field is unused by the legacy adapter
# (the wire framing comes from the payload dict). Pass a single
# constant so the producer side stays free of dummy strings.
_FRAME = "frame"


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

    Routes through ``helpers.event_bus.stream_events`` for the
    core pipeline (subscribe, bounded drain, listener cleanup on
    cancel). Every wire frame travels the same path —
    ``handle_event`` and ``send_initial`` build a legacy-frame
    dict and push it through ``controls``; ``stream_events``
    drains the queue and ``_LegacyWSWriter`` writes each dict to
    the WS verbatim. No direct ``ws.send_json`` calls outside the
    adapter.

    The snapshot/subscribe race is closed at the call site:

    1. ``snapshot = list(job.output)`` is captured *outside*
       ``stream_events`` — a fresh list copy so a later
       ``_trim_job_output`` reassign of ``job.output``
       (``firmware/helpers.py``) doesn't mutate what we replay.
    2. ``stream_events`` attaches the listener (sync) before any
       ``await`` yields control. No event can fire between the
       snapshot in (1) and the listener attach in (2), so each
       line lands in exactly one of the two — never both, never
       neither.
    3. ``send_initial`` pushes the snapshot frames synchronously
       (no awaits between pushes) so they can't interleave with
       live events that the listener queues after subscribe.

    Capacity: ``stream_events`` defaults to a 4000-slot queue.
    Lines push with drop-on-full (slow follower → drop, no
    bus.fire blocking); the terminal frame pushes with
    ``push_priority`` (force-enqueue, evicts oldest) so the
    exit frame always lands even if the queue is saturated.
    """
    job_id = job.job_id
    snapshot = list(job.output)
    initial_status = job.status
    initial_exit_code = job.exit_code

    async def _send_initial(controls: StreamControls) -> None:
        for line in snapshot:
            controls.push(_FRAME, _line_frame(line))
        # ``compile`` / ``upload`` may resolve a job that's already
        # in a terminal state — most common on a duplicate-submit
        # supersede that lands the previous job in CANCELLED before
        # the new one is created. Push the exit frame from the
        # cached status and short-circuit the drain.
        if initial_status in TERMINAL_JOB_STATUSES:
            controls.push_priority(_FRAME, _exit_frame(initial_exit_code))
            controls.end()

    def _handle_event(event: Event, controls: StreamControls) -> None:
        if event.event_type == EventType.JOB_OUTPUT:
            if event.data.get("job_id") == job_id:
                controls.push(_FRAME, _line_frame(event.data.get("line", "")))
            return
        # Narrow the untyped ``event.data["job"]`` to a real
        # ``FirmwareJob`` so the type checker sees direct
        # attribute access for ``job_id`` / ``exit_code``. The
        # runner fires terminal events as ``{"job": job}``;
        # anything that doesn't satisfy the isinstance check is
        # silently ignored rather than tearing the WS down with
        # an ``AttributeError``.
        ev_job = event.data.get("job")
        if not isinstance(ev_job, FirmwareJob) or ev_job.job_id != job_id:
            return
        controls.push_priority(_FRAME, _exit_frame(ev_job.exit_code))
        controls.end()

    await stream_events(
        client=_LegacyWSWriter(ws),
        message_id="",
        bus=bus,
        event_types=(EventType.JOB_OUTPUT, *TERMINAL_JOB_EVENTS),
        handle_event=_handle_event,
        send_initial=_send_initial,
    )


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
    db: DeviceBuilder = request.app["device_builder"]

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

        await _handle_spawn(ws, db, job_type, data)
        break

    return ws


async def _handle_spawn(
    ws: web.WebSocketResponse,
    db: DeviceBuilder,
    job_type: JobType,
    data: dict[str, Any],
) -> None:
    """Run one spawn message: validate, submit, stream until terminal.

    The startup-order invariant (``DeviceBuilder.start()`` populates
    every controller before HTTP requests are served) is pinned by
    ``tests/test_device_builder_lifecycle.py::test_start_initialises_all_controllers``.
    Reading ``db.firmware`` here (rather than at the top of the
    receive handler) keeps no-op connections — clients that close
    immediately or send only binary frames — from touching the
    controller at all. The ``assert`` narrows
    ``Optional[FirmwareController]`` for the call sites below
    without ``# type: ignore`` shims.

    Three rejection paths all surface to HA via the protocol's
    only signalling channel — a ``code: 1`` exit frame:

    1. Non-string ``configuration`` / ``port`` (an explicit
       ``null`` or a JSON number / object would otherwise crash
       the firmware controller's path validation with a
       ``TypeError`` / ``AttributeError`` that aiohttp would
       surface as a connection drop).
    2. ``CommandError`` from ``_validate_configuration_boundary``
       (traversal, empty configuration, etc.).
    3. Implicit on success after the streaming finishes.
    """
    firmware = db.firmware
    bus = db.bus
    assert firmware is not None

    configuration = data.get("configuration", "")
    port = data.get("port", "") if job_type is JobType.UPLOAD else ""
    if not isinstance(configuration, str) or not isinstance(port, str):
        await ws.send_json(_exit_frame(1), dumps=dumps_str)
        return

    try:
        if job_type is JobType.UPLOAD:
            job = await firmware.upload(configuration=configuration, port=port)
        else:
            job = await firmware.compile(configuration=configuration)
    except CommandError:
        await ws.send_json(_exit_frame(1), dumps=dumps_str)
        return

    await _stream_job_to_legacy_ws(ws, bus, job)


async def _json_config_response(db: DeviceBuilder, configuration: str) -> web.Response:
    """Resolve *configuration* via ``esphome config`` and return it as JSON.

    403 traversal, 404 missing file, 500 no esphome, 503 infra fault, 422 invalid.
    """
    try:
        # ``rel_path`` calls ``Path.resolve``, a blocking syscall — run it in
        # the executor so blockbuster doesn't fault the request on CI.
        config_path = await run_in_executor(db.settings.rel_path, configuration)
    except CommandError:
        return json_response({"error": "Forbidden"}, status=403)

    if not await run_in_executor(config_path.is_file):
        return json_response({"error": "Not found"}, status=404)

    devices = db.devices
    esphome_cmd = devices.state.esphome_cmd if devices is not None else []
    if not esphome_cmd:
        return json_response({"error": "esphome unavailable"}, status=500)

    # 503 (retryable) for an infra fault vs 422 for a config that ran and failed;
    # the body stays generic either way — an invalid config's error can carry
    # resolved secrets (``--show-secrets``).
    try:
        config = await run_esphome_config(esphome_cmd, config_path)
    except EsphomeConfigUnavailableError:
        return json_response({"error": "esphome config unavailable"}, status=503)
    if config is None:
        return json_response({"error": "Configuration is invalid"}, status=422)
    return json_response(config)


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
        configured_names = {d.get("name") for d in configured}

        importable = [
            imp.to_dict()
            for name, imp in devices_ctrl.state.import_result.items()
            if name not in devices_ctrl.state.ignored_devices and name not in configured_names
        ]

        return json_response({"configured": configured, "importable": importable})

    @routes.get("/ping")
    async def legacy_ping(request: web.Request) -> web.Response:
        """Legacy online-status map ``{<config>.yaml: True|False|None}`` (third-party widgets)."""
        db = request.app["device_builder"]
        devices_ctrl = db.devices
        await devices_ctrl.poll()
        return json_response(
            {d.configuration: _STATE_TO_BOOL.get(d.state) for d in devices_ctrl.get_devices()}
        )

    @routes.get("/json-config")
    async def legacy_json_config(request: web.Request) -> web.Response:
        """Legacy GET /json-config — fully-resolved config as JSON.

        Resolves substitutions / packages / includes / secrets via
        ``esphome config --show-secrets`` (same path HA's encryption-key
        detection needs), so a raw ``load_yaml`` can't leak unresolved
        ``${vars}`` / unmerged packages or 500 on ``EFloat`` / ``!lambda``.
        """
        db = request.app["device_builder"]
        return await _json_config_response(db, request.query.get("configuration", ""))

    @routes.get("/compile")
    async def legacy_compile(request: web.Request) -> web.WebSocketResponse:
        return await _handle_legacy_ws_command(request, JobType.COMPILE)

    @routes.get("/upload")
    async def legacy_upload(request: web.Request) -> web.WebSocketResponse:
        return await _handle_legacy_ws_command(request, JobType.UPLOAD)

    return routes
