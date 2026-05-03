"""DEPRECATED: Legacy REST + WebSocket endpoints for Home Assistant compatibility.

These endpoints exist only for backward compatibility with the HA ESPHome
integration (via esphome-dashboard-api). They will be removed once HA
migrates to the /ws multiplexed API.

HA uses:
- GET /devices (list configured + importable devices)
- GET /json-config?configuration=... (parsed YAML as JSON)
- /compile (WebSocket, spawn protocol)
- /upload (WebSocket, spawn protocol)
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

import aiohttp
from aiohttp import web
from esphome import yaml_util

from ..helpers.api import CommandError
from ..helpers.json import (
    JSONDecodeError,
    dumps_str,
    dumps_str_non_str_keys,
    json_response,
    loads,
)
from ..helpers.subprocess import create_subprocess_exec

_LOGGER = logging.getLogger(__name__)

_ESPHOME_CMD = [sys.executable, "-m", "esphome"]


async def _handle_legacy_ws_command(
    request: web.Request, command: str, extra_args_fn: Any = None
) -> web.WebSocketResponse:
    """Legacy spawn-based WebSocket handler."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    async for msg in ws:
        if msg.type != aiohttp.WSMsgType.TEXT:
            break

        try:
            data = loads(msg.data)
        except JSONDecodeError:
            # Legacy clients shouldn't send non-JSON, but if one does
            # we'd rather skip the frame than tear down the whole
            # spawn handler with the next iteration losing its
            # subprocess output.
            _LOGGER.debug("Ignoring non-JSON frame on %s", request.path)
            continue
        if not isinstance(data, dict) or data.get("type") != "spawn":
            continue

        configuration = data.get("configuration", "")
        settings = request.app["device_builder"].settings
        loop = asyncio.get_running_loop()
        try:
            # ``rel_path`` calls ``Path.resolve``, a blocking syscall —
            # off-loop so blockbuster doesn't fault the request on CI.
            resolved = await loop.run_in_executor(None, settings.rel_path, configuration)
            config_path = str(resolved)
        except CommandError:
            # Send a controlled exit frame instead of letting the
            # ``CommandError`` tear the WebSocket down — the legacy
            # spawn protocol uses ``{event: "exit", code}`` as its
            # only signalling channel, so this is what HA's
            # esphome-dashboard-api expects to see on rejection.
            await ws.send_json({"event": "exit", "code": 1}, dumps=dumps_str)
            break
        cmd = [*_ESPHOME_CMD, command, config_path]
        if extra_args_fn:
            cmd.extend(extra_args_fn(data))

        proc = await create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert proc.stdout is not None  # type narrowing
        async for line in proc.stdout:
            await ws.send_json(
                {"event": "line", "data": line.decode("utf-8", errors="replace")},
                dumps=dumps_str,
            )
        exit_code = await proc.wait()
        await ws.send_json({"event": "exit", "code": exit_code}, dumps=dumps_str)
        break

    return ws


def create_legacy_routes() -> web.RouteTableDef:
    """Create backward-compatible REST + WS routes for HA."""
    routes = web.RouteTableDef()

    @routes.get("/devices")
    async def legacy_devices(request: web.Request) -> web.Response:
        """Legacy GET /devices — returns configured + importable devices."""
        db = request.app["device_builder"]
        devices_ctrl = db.devices
        await devices_ctrl._request_scan()

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
        return await _handle_legacy_ws_command(request, "compile")

    @routes.get("/upload")
    async def legacy_upload(request: web.Request) -> web.WebSocketResponse:
        def _extra_args(data: dict) -> list[str]:
            port = data.get("port", "")
            return ["--device", port] if port else []

        return await _handle_legacy_ws_command(request, "upload", _extra_args)

    return routes
