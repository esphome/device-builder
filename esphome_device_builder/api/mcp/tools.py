"""Device Builder's MCP tools: each wraps a WS command and returns a text result."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import TYPE_CHECKING, Any

from esphome.const import CONF_EXTERNAL_COMPONENTS, CONF_PACKAGES, CONF_SUBSTITUTIONS
from esphome.core import EsphomeError

from ...constants import is_secrets_file
from ...controllers.auth import AuthError
from ...controllers.devices.helpers import raise_device_not_found, require_catalog
from ...controllers.firmware.follow import initial_snapshot
from ...controllers.firmware.persistence import job_dict_without_output
from ...helpers.ansi import ANSI_CSI_RE
from ...helpers.api import CommandError
from ...helpers.async_ import run_in_executor
from ...helpers.device_yaml import (
    ESPHOME_CONFIG_TIMEOUT,
    extract_component_ids,
    load_device_yaml_strict,
)
from ...mcp import INTERNAL_ERROR, McpToolError, ToolRegistry
from ...models import ErrorCode, StreamEvent

if TYPE_CHECKING:
    from pathlib import Path

    from ...controllers.config import DashboardSettings
    from ...device_builder import DeviceBuilder

_MESSAGE_ID = "mcp"
_DEFAULT_TAIL_LINES = 50
_MAX_TAIL_LINES = 1000
_MAX_SEARCH_RESULTS = 100
# Config metadata blocks that are not components and have no catalog entry.
_NON_COMPONENT_KEYS = frozenset({CONF_SUBSTITUTIONS, CONF_PACKAGES, CONF_EXTERNAL_COMPONENTS})
# Bulk of a device row; ``get_config_components`` covers them per device.
_LIST_DEVICES_OMIT = frozenset(
    {"loaded_integrations", "loaded_platforms", "directly_referenced_integrations"}
)


def _translate(err: Exception) -> McpToolError | None:
    """Map the user-facing WS command errors onto ``McpToolError``; anything else is internal."""
    if isinstance(err, (CommandError, AuthError)):
        return McpToolError(err.code.value, err.message)
    return None


TOOLS: ToolRegistry[DeviceBuilder] = ToolRegistry(translate=_translate)
_tool = TOOLS.tool


def _prop(json_type: str, description: str) -> dict[str, str]:
    return {"type": json_type, "description": description}


_CONFIGURATION = _prop(
    "string", "Device YAML filename, e.g. 'living-room.yaml' (from list_devices)."
)
_COMPONENT_ID = _prop("string", "Catalog id, e.g. 'sensor.dht' or 'wifi'.")
_JOB_ID = _prop("string", "Firmware job id returned by compile or install.")


class CollectingClient:
    """Stream-client stand-in keeping the last output lines and the result frame of one call."""

    def __init__(self, tail: int = _DEFAULT_TAIL_LINES) -> None:
        self.output: deque[str] = deque(maxlen=tail)
        self.result: dict[str, Any] | None = None

    async def send_event(self, _message_id: str, event: str, data: Any = None) -> None:
        if event == StreamEvent.OUTPUT:
            self.output.append(data)
        elif event == StreamEvent.RESULT:
            self.result = data

    def register_stream(self, message_id: str, task: Any) -> None: ...

    def unregister_stream(self, message_id: str) -> None: ...


async def _call(
    db: DeviceBuilder, command: str, *, client: CollectingClient | None = None, **args: Any
) -> Any:
    """Invoke a WS command handler; *client* receives any stream frames."""
    handler = db.command_handlers.get(command)
    if handler is None:
        raise CommandError(ErrorCode.UNAVAILABLE, f"{command} is not available")
    return await handler(client=client or CollectingClient(), message_id=_MESSAGE_ID, **args)


def _prune(value: Any, *, include_advanced: bool = False) -> Any:
    """
    Drop ``None`` / ``False`` / empty values from a serialised catalog model, recursively.

    Nested ``config_entries`` lose ``hidden`` entries, and ``advanced`` ones
    unless *include_advanced*. ``0`` is kept.
    """
    if isinstance(value, list):
        return [_prune(item, include_advanced=include_advanced) for item in value]
    if not isinstance(value, dict):
        return value
    if isinstance(value.get("config_entries"), list):
        value = value | {
            "config_entries": [
                entry
                for entry in value["config_entries"]
                if not (entry.get("hidden") or (entry.get("advanced") and not include_advanced))
            ]
        }
    pruned = {k: _prune(v, include_advanced=include_advanced) for k, v in value.items()}
    return {k: v for k, v in pruned.items() if not _is_empty(v)}


def _is_empty(value: Any) -> bool:
    return value is None or value is False or (isinstance(value, (str, list, dict)) and not value)


def _tail(lines: list[str], count: int) -> list[str]:
    """Last *count* output lines with ANSI colour and line terminators stripped."""
    if count <= 0:
        return []
    return [ANSI_CSI_RE.sub("", line).rstrip("\r\n") for line in lines[-count:]]


def _device_configuration(configuration: str) -> str:
    """Refuse the shared secrets file: its contents must never reach a model."""
    if is_secrets_file(configuration):
        raise CommandError(ErrorCode.INVALID_ARGS, "secrets.yaml is not available over MCP")
    return configuration


def _load_strict(settings: DashboardSettings, configuration: str) -> dict:
    """Executor half of ``get_config_components``; ``rel_path`` walks the filesystem."""
    path: Path = settings.rel_path(configuration)
    if not path.is_file():
        raise FileNotFoundError(configuration)
    return load_device_yaml_strict(path)


@_tool(
    "list_devices",
    "List configured ESPHome devices with their online state, address and deployed "
    "firmware version.",
)
async def _list_devices(db: DeviceBuilder, _args: dict[str, Any]) -> list[dict[str, Any]]:
    response = await _call(db, "devices/list")
    return [
        {k: v for k, v in device.to_flat_dict().items() if k not in _LIST_DEVICES_OMIT}
        for device in response.configured
    ]


@_tool(
    "get_config",
    "Read a device's YAML configuration.",
    {"configuration": _CONFIGURATION},
    ("configuration",),
)
async def _get_config(db: DeviceBuilder, args: dict[str, Any]) -> Any:
    _device_configuration(args["configuration"])
    return await _call(db, "devices/get_config", **args)


@_tool(
    "update_config",
    "Replace a device's YAML configuration with new content. Read it with get_config "
    "first and change only what is needed; run validate_config or compile afterwards.",
    {
        "configuration": _CONFIGURATION,
        "content": _prop("string", "The complete new YAML."),
    },
    ("configuration", "content"),
)
async def _update_config(db: DeviceBuilder, args: dict[str, Any]) -> str:
    _device_configuration(args["configuration"])
    await _call(db, "devices/update_config", **args)
    return f"Saved {args['configuration']}"


@_tool(
    "add_component",
    "Add a catalog component to a device config and save it. Use search_components to "
    "find the id and get_component for its fields.",
    {
        "configuration": _CONFIGURATION,
        "component_id": _COMPONENT_ID,
        "fields": _prop(
            "object", "Config values keyed by field name; nested blocks are nested objects."
        ),
    },
    ("configuration", "component_id"),
)
async def _add_component(db: DeviceBuilder, args: dict[str, Any]) -> Any:
    response = await _call(db, "devices/add_component", **args)
    return {"configuration": args["configuration"], "component_id": args["component_id"]} | (
        response.to_dict()
    )


@_tool(
    "validate_config",
    "Validate a device config with esphome and return the output. Bounded to one minute; "
    "a timed out run reports timed_out.",
    {"configuration": _CONFIGURATION},
    ("configuration",),
)
async def _validate_config(db: DeviceBuilder, args: dict[str, Any]) -> dict[str, Any]:
    client = CollectingClient()
    # The stream helper swallows the cancel and returns, so the deadline is read explicitly.
    timed_out = False
    try:
        async with asyncio.timeout(ESPHOME_CONFIG_TIMEOUT) as deadline:
            await _call(db, "devices/validate", client=client, **args)
        timed_out = bool(deadline.expired())
    except TimeoutError:
        timed_out = True
    output = _tail(list(client.output), _DEFAULT_TAIL_LINES)
    if timed_out:
        return {"success": False, "timed_out": True, "output": output}
    if client.result is None:
        raise McpToolError(INTERNAL_ERROR, "Validation produced no result")
    return {
        "success": client.result.get("success", False),
        "exit_code": client.result.get("code"),
        "output": output,
    }


@_tool(
    "compile",
    "Queue a firmware compile for a device. Returns a job id to poll with get_job.",
    {"configuration": _CONFIGURATION},
    ("configuration",),
)
async def _compile(db: DeviceBuilder, args: dict[str, Any]) -> dict[str, Any]:
    job = await _call(db, "firmware/compile", **args)
    return {"job_id": job.job_id, "status": job.status}


@_tool(
    "install",
    "Compile and install firmware on a device. Returns the compile job id and the "
    "dependent upload job id; poll both with get_job. An offline device gets the update "
    "queued for its next wake (deferred, no upload job).",
    {
        "configuration": _CONFIGURATION,
        "port": _prop("string", "'OTA' (default), a serial port, or an IP/hostname."),
    },
    ("configuration",),
)
async def _install(db: DeviceBuilder, args: dict[str, Any]) -> dict[str, Any]:
    job = await _call(db, "firmware/install", **args)
    siblings = await _call(db, "firmware/get_jobs", configuration=args["configuration"])
    upload = next((j for j in siblings if j.depends_on == job.job_id), None)
    if upload is None and not job.is_deferred_install:
        raise McpToolError(INTERNAL_ERROR, f"Install chain for {job.job_id} has no upload job")
    return {
        "job_id": job.job_id,
        "status": job.status,
        "upload_job_id": upload.job_id if upload else None,
        "deferred": job.is_deferred_install,
    }


@_tool(
    "get_job",
    "Get a firmware job's status, progress, exit code and the last output lines.",
    {
        "job_id": _JOB_ID,
        "tail_lines": _prop(
            "integer", f"Output lines to return from the end (default 50, max {_MAX_TAIL_LINES})."
        ),
    },
    ("job_id",),
)
async def _get_job(db: DeviceBuilder, args: dict[str, Any]) -> dict[str, Any]:
    job = await _call(db, "firmware/get_job", job_id=args["job_id"])
    if job is None:
        raise CommandError(ErrorCode.NOT_FOUND, f"Job not found: {args['job_id']}")
    tail_lines = min(args.get("tail_lines", _DEFAULT_TAIL_LINES), _MAX_TAIL_LINES)
    output = await initial_snapshot(job, job.job_id) if tail_lines > 0 else []
    return job_dict_without_output(job) | {
        "queued_update_armed": job.is_queued_update_armed,
        "output": _tail(output, tail_lines),
    }


@_tool(
    "cancel_job",
    "Cancel a queued or running firmware job.",
    {"job_id": _JOB_ID},
    ("job_id",),
)
async def _cancel_job(db: DeviceBuilder, args: dict[str, Any]) -> str:
    await _call(db, "firmware/cancel", **args)
    return f"Cancelled {args['job_id']}"


@_tool(
    "search_components",
    "Search the ESPHome component catalog by name or keyword.",
    {
        "query": _prop("string", "Search text."),
        "limit": _prop("integer", f"Max results (default 20, max {_MAX_SEARCH_RESULTS})."),
    },
    ("query",),
)
async def _search_components(db: DeviceBuilder, args: dict[str, Any]) -> list[dict[str, Any]]:
    limit = min(args.get("limit", 20), _MAX_SEARCH_RESULTS)
    response = await _call(db, "components/get_components", **{**args, "limit": limit})
    return [_prune(entry.to_dict()) for entry in response.components]


@_tool(
    "get_component",
    "Get a component's documentation: description, docs URL and every config field with "
    "type, description, default, required flag and allowed values. Omitted booleans are "
    "false and omitted values are empty. Advanced fields are omitted unless "
    "include_advanced is true.",
    {
        "component_id": _COMPONENT_ID,
        "platform": _prop(
            "string", "Target platform (esp32, esp8266, ...) to resolve platform defaults."
        ),
        "include_advanced": _prop("boolean", "Include advanced fields."),
    },
    ("component_id",),
)
async def _get_component(db: DeviceBuilder, args: dict[str, Any]) -> Any:
    component_id = args["component_id"]
    bodies = await _call(
        db,
        "components/get_component_bodies",
        component_ids=[component_id],
        platform=args.get("platform"),
    )
    if component_id not in bodies:
        raise CommandError(ErrorCode.NOT_FOUND, f"Unknown component: {component_id}")
    return _prune(
        bodies[component_id].to_dict(), include_advanced=args.get("include_advanced", False)
    )


@_tool(
    "get_config_components",
    "List the components a device config uses, with a short description and docs URL for "
    "each. Call get_component for the fields of any of them. Resolving a config that pulls "
    "a package for the first time can take longer than ten seconds.",
    {"configuration": _CONFIGURATION},
    ("configuration",),
)
async def _get_config_components(db: DeviceBuilder, args: dict[str, Any]) -> list[dict[str, Any]]:
    catalog = require_catalog(db)
    configuration = _device_configuration(args["configuration"])
    try:
        async with asyncio.timeout(ESPHOME_CONFIG_TIMEOUT):
            config = await run_in_executor(_load_strict, db.settings, configuration)
    except FileNotFoundError as err:
        raise_device_not_found(configuration, from_exc=err)
    except EsphomeError as err:
        raise CommandError(ErrorCode.INVALID_ARGS, f"{configuration}: {err}") from err
    except TimeoutError as err:
        msg = f"Resolving {configuration} exceeded {ESPHOME_CONFIG_TIMEOUT:.0f}s"
        raise CommandError(ErrorCode.UNAVAILABLE, msg) from err
    rows = []
    for component_id in extract_component_ids(config):
        if component_id in _NON_COMPONENT_KEYS:
            continue
        entry = catalog.index_entry(component_id)
        rows.append({"id": component_id} | (_prune(entry.to_dict()) if entry else {}))
    return rows
