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
import json
import logging
import sys
from typing import Any

import aiohttp
from aiohttp import web
from esphome import yaml_util

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

        data = json.loads(msg.data)
        if data.get("type") != "spawn":
            continue

        configuration = data.get("configuration", "")
        settings = request.app["device_builder"].settings
        config_path = str(settings.rel_path(configuration))
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
            await ws.send_json({"event": "line", "data": line.decode("utf-8", errors="replace")})
        exit_code = await proc.wait()
        await ws.send_json({"event": "exit", "code": exit_code})
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

        return web.json_response({"configured": configured, "importable": importable})

    @routes.get("/json-config")
    async def legacy_json_config(request: web.Request) -> web.Response:
        """Legacy GET /json-config — parsed YAML config as JSON."""
        configuration = request.query.get("configuration", "")
        db = request.app["device_builder"]
        try:
            config_path = db.settings.rel_path(configuration)
        except ValueError:
            return web.json_response({"error": "Forbidden"}, status=403)

        loop = asyncio.get_running_loop()
        try:
            config = await loop.run_in_executor(None, yaml_util.load_yaml, str(config_path))
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

        return web.json_response(config)

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
