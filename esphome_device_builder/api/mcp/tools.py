"""Device Builder's MCP tools: each wraps a WS command and returns a text result."""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ...constants import SECRETS_FILENAMES, is_device_config_name, is_secrets_file
from ...controllers.automations.catalog import AUTOMATION_TYPES
from ...controllers.devices.helpers import scanned_component_entries
from ...controllers.firmware.follow import initial_snapshot
from ...controllers.firmware.persistence import job_dict_without_output
from ...helpers.ansi import plain_lines
from ...helpers.api import CollectingClient, CommandError
from ...mcp import INTERNAL_ERROR, McpToolError, ToolRegistry
from ...models import ErrorCode

if TYPE_CHECKING:
    from ...device_builder import DeviceBuilder
    from ...mcp.tools import ToolHandler

_MESSAGE_ID = "mcp"
_LOGGER = logging.getLogger(__name__)


def _translate(err: Exception) -> McpToolError | None:
    """Map a user-facing WS command error onto ``McpToolError``; anything else is internal."""
    if isinstance(err, CommandError):
        return McpToolError(err.code.value, err.message)
    return None


TOOLS: ToolRegistry[DeviceBuilder] = ToolRegistry(translate=_translate)


def _tool(
    name: str,
    description: str,
    properties: dict[str, dict[str, Any]] | None = None,
    required: tuple[str, ...] = (),
    *,
    reads_secrets: bool = False,
) -> Callable[[ToolHandler[DeviceBuilder]], ToolHandler[DeviceBuilder]]:
    """Register a tool; only a *reads_secrets* tool takes the secrets file as ``configuration``."""

    def register(handler: ToolHandler[DeviceBuilder]) -> ToolHandler[DeviceBuilder]:
        async def guarded(db: DeviceBuilder, args: dict[str, Any]) -> Any:
            _check_configuration(args.get("configuration"), allow_secrets=reads_secrets)
            return await handler(db, args)

        TOOLS.tool(name, description, properties, required)(guarded)
        return guarded

    return register


def _prop(json_type: str, description: str) -> dict[str, str]:
    return {"type": json_type, "description": description}


_CONFIGURATION = _prop(
    "string", "Device YAML filename, e.g. 'living-room.yaml' (from list_devices)."
)
_COMPONENT_ID = _prop("string", "Catalog id, e.g. 'sensor.dht' or 'wifi'.")
_JOB_ID = _prop("string", "Firmware job id returned by compile or install.")
_TAIL_LINES = {
    "type": "integer",
    "description": "Output lines to keep from the end.",
    "minimum": 0,
    "maximum": 1000,
    "default": 50,
}
_LIMIT = {
    "type": "integer",
    "description": "Max results.",
    "minimum": 1,
    "maximum": 100,
    "default": 20,
}


@_tool(
    "list_devices",
    "List configured ESPHome devices with their online state, address and deployed "
    "firmware version. Scalar fields only; per-device lists such as loaded integrations "
    "come from get_config_components.",
)
async def _list_devices(db: DeviceBuilder, _args: dict[str, Any]) -> list[dict[str, Any]]:
    response = await _call(db, "devices/list")
    return [
        {k: v for k, v in device.to_flat_dict().items() if not isinstance(v, list)}
        for device in response.configured
    ]


@_tool(
    "get_config",
    "Read a device's YAML configuration, or secrets.yaml.",
    {"configuration": _CONFIGURATION},
    ("configuration",),
    reads_secrets=True,
)
async def _get_config(db: DeviceBuilder, args: dict[str, Any]) -> Any:
    return await _call(db, "devices/get_config", **_only(args, "configuration"))


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
    await _call(db, "devices/update_config", **_only(args, "configuration", "content"))
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
    response = await _call(
        db, "devices/add_component", **_only(args, "configuration", "component_id", "fields")
    )
    return response.to_dict()


@_tool(
    "validate_config",
    "Validate a device config with esphome and return the last output lines (truncated "
    "says whether earlier lines were dropped; esphome's concealed values show as <removed>).",
    {"configuration": _CONFIGURATION, "tail_lines": _TAIL_LINES},
    ("configuration",),
)
async def _validate_config(db: DeviceBuilder, args: dict[str, Any]) -> dict[str, Any]:
    client = CollectingClient(tail=args["tail_lines"])
    await _call(db, "devices/validate", client=client, configuration=args["configuration"])
    if (result := client.result) is None:
        _LOGGER.error("MCP validate of %s produced no result frame", args["configuration"])
        raise McpToolError(INTERNAL_ERROR, "Validation produced no result")
    return {
        "success": result["success"],
        "exit_code": result["code"],
        "output": plain_lines(list(client.output)),
        "truncated": client.truncated,
    }


@_tool(
    "compile",
    "Queue a firmware compile for a device. Returns a job id to poll with get_job.",
    {"configuration": _CONFIGURATION},
    ("configuration",),
)
async def _compile(db: DeviceBuilder, args: dict[str, Any]) -> dict[str, Any]:
    job = await _call(db, "firmware/compile", **_only(args, "configuration"))
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
    job = await _call(db, "firmware/install", **_only(args, "configuration", "port"))
    siblings = await _call(db, "firmware/get_jobs", configuration=args["configuration"])
    upload = next((j for j in siblings if j.depends_on == job.job_id), None)
    if upload is None and not job.is_deferred_install:
        _LOGGER.error("MCP install chain for %s has no upload job", job.job_id)
        msg = (
            f"Compile job {job.job_id} is queued but its install chain has no upload job; "
            "poll it with get_job instead of retrying install"
        )
        raise McpToolError(INTERNAL_ERROR, msg)
    return {
        "job_id": job.job_id,
        "status": job.status,
        "upload_job_id": upload.job_id if upload else None,
        "deferred": job.is_deferred_install,
    }


@_tool(
    "get_job",
    "Get a firmware job's status, progress, exit code and the last output lines.",
    {"job_id": _JOB_ID, "tail_lines": _TAIL_LINES},
    ("job_id",),
)
async def _get_job(db: DeviceBuilder, args: dict[str, Any]) -> dict[str, Any]:
    tail_lines = args["tail_lines"]
    job = await _call(db, "firmware/get_job", job_id=args["job_id"])
    if job is None:
        raise CommandError(ErrorCode.NOT_FOUND, f"Job not found: {args['job_id']}")
    snapshot = await initial_snapshot(job, job.job_id)
    lines = snapshot or []
    output = list(deque(lines, maxlen=tail_lines))
    return job_dict_without_output(job) | {
        "queued_update_armed": job.is_queued_update_armed,
        "output": plain_lines(output),
        "truncated": len(lines) > len(output),
        "output_available": snapshot is not None,
    }


@_tool(
    "cancel_job",
    "Cancel a queued or running firmware job.",
    {"job_id": _JOB_ID},
    ("job_id",),
)
async def _cancel_job(db: DeviceBuilder, args: dict[str, Any]) -> str:
    await _call(db, "firmware/cancel", **_only(args, "job_id"))
    return f"Cancelled {args['job_id']}"


@_tool(
    "search_components",
    "Search the ESPHome component catalog by name or keyword.",
    {
        "query": _prop("string", "Search text."),
        "limit": _LIMIT,
    },
    ("query",),
)
async def _search_components(db: DeviceBuilder, args: dict[str, Any]) -> list[dict[str, Any]]:
    response = await _call(
        db, "components/get_components", query=args["query"], limit=args["limit"]
    )
    return [_prune(entry.to_dict()) for entry in response.components]


@_tool(
    "get_component",
    "Get a component's documentation: description, docs URL and every config field with "
    "type, description, required flag and allowed values. default_value is what ESPHome "
    "uses when the key is absent; an omitted flag is false. Advanced fields are omitted "
    "unless include_advanced is true.",
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
    "List the catalog components a device config used as of its last scan (every save "
    "rescans), with a short description and docs URL for each. Call get_component for "
    "the fields of any of them.",
    {"configuration": _CONFIGURATION},
    ("configuration",),
)
async def _get_config_components(db: DeviceBuilder, args: dict[str, Any]) -> list[dict[str, Any]]:
    entries = scanned_component_entries(db, args["configuration"])
    return [_prune(entry.to_dict()) for entry in entries]


@_tool(
    "search_boards",
    "Search the board catalog by name or chip; returns board ids for create_device.",
    {
        "query": _prop("string", "Search text, e.g. 'esp32-c3' or 'nodemcu'."),
        "limit": _LIMIT,
    },
    ("query",),
)
async def _search_boards(db: DeviceBuilder, args: dict[str, Any]) -> list[dict[str, Any]]:
    response = await _call(db, "boards/get_boards", query=args["query"], limit=args["limit"])
    return [_prune(board.to_dict()) for board in response.boards]


@_tool(
    "list_secret_names",
    "List the secret names defined in secrets.yaml. Reference one in YAML as '!secret <name>'.",
)
async def _list_secret_names(db: DeviceBuilder, _args: dict[str, Any]) -> Any:
    return await _call(db, "config/get_secrets")


@_tool(
    "set_secret",
    "Create or update one secret in secrets.yaml, the only way to change that file. "
    "Reference the secret in YAML as '!secret <name>'.",
    {
        "name": _prop("string", "Secret name, e.g. 'wifi_password'."),
        "value": _prop("string", "The secret value."),
        "overwrite": _prop("boolean", "Replace an existing value (default true)."),
    },
    ("name", "value"),
)
async def _set_secret(db: DeviceBuilder, args: dict[str, Any]) -> dict[str, Any]:
    result = await _call(
        db,
        "config/set_secret",
        key=args["name"],
        value=args["value"],
        overwrite=args.get("overwrite", True),
    )
    return {"name": args["name"], "created": result["created"]}


@_tool(
    "create_device",
    "Create a new device YAML with the wizard. Wi-Fi is written as '!secret wifi_ssid' and "
    "'!secret wifi_password' (set them first with set_secret if list_secret_names lacks them); "
    "it takes no Wi-Fi arguments. Returns the new configuration filename.",
    {
        "name": _prop("string", "Device name (its hostname), e.g. 'living-room-sensor'."),
        "friendly_name": _prop("string", "Human readable name."),
        "board_id": _prop("string", "Board id from search_boards, e.g. 'esp32dev'."),
    },
    ("name",),
)
async def _create_device(db: DeviceBuilder, args: dict[str, Any]) -> Any:
    response = await _call(db, "devices/create", **_only(args, "name", "friendly_name", "board_id"))
    return response.to_dict()


@_tool(
    "list_automations",
    "List the automations a device config contains (scripts, intervals, api actions, "
    "on_* triggers, light effects) with their YAML, line range and location. Edit them by "
    "rewriting the YAML with update_config; remove one with delete_automation.",
    {"configuration": _CONFIGURATION},
    ("configuration",),
)
async def _list_automations(db: DeviceBuilder, args: dict[str, Any]) -> Any:
    rows = await _call(db, "automations/parse", **_only(args, "configuration"))
    # The decomposed tree serves the visual editor; the model edits the YAML.
    return _prune([{k: v for k, v in row.items() if k != "automation"} for row in rows])


@_tool(
    "get_available_automations",
    "The triggers, actions, conditions, scripts and component instances this device's "
    "config makes available for automations, by id.",
    {"configuration": _CONFIGURATION},
    ("configuration",),
)
async def _get_available_automations(db: DeviceBuilder, args: dict[str, Any]) -> Any:
    return _prune(await _call(db, "automations/get_available", **_only(args, "configuration")))


@_tool(
    "get_automation_docs",
    "Documentation for automation building blocks: each ref is {type, id} with type one of "
    + ", ".join(AUTOMATION_TYPES)
    + " and id from get_available_automations, e.g. {type: 'actions', id: 'light.turn_on'}.",
    {
        "refs": _prop("array", "List of {type, id} refs."),
        "include_advanced": _prop("boolean", "Include advanced fields."),
    },
    ("refs",),
)
async def _get_automation_docs(db: DeviceBuilder, args: dict[str, Any]) -> Any:
    include_advanced = args.get("include_advanced", False)
    for ref in args["refs"]:
        if (
            not isinstance(ref, dict)
            or ref.get("type") not in AUTOMATION_TYPES
            or not isinstance(ref.get("id"), str)
            or not ref["id"]
        ):
            msg = f"each ref needs a type of {', '.join(AUTOMATION_TYPES)} and an id"
            raise CommandError(ErrorCode.INVALID_ARGS, msg)
    bodies = await _call(db, "automations/get_bodies", refs=args["refs"])
    if missing := [
        f"{ref['type']}/{ref['id']}"
        for ref in args["refs"]
        if f"{ref['type']}/{ref['id']}" not in bodies
    ]:
        raise CommandError(ErrorCode.NOT_FOUND, f"Unknown automation refs: {', '.join(missing)}")
    return _prune(bodies, include_advanced=include_advanced)


@_tool(
    "delete_automation",
    "Remove one automation from a device config and save it; pass the location from "
    "list_automations.",
    {
        "configuration": _CONFIGURATION,
        "location": _prop("object", "The automation's location as returned by list_automations."),
    },
    ("configuration", "location"),
)
async def _delete_automation(db: DeviceBuilder, args: dict[str, Any]) -> str:
    await _call(db, "automations/delete", save=True, **_only(args, "configuration", "location"))
    return f"Removed the automation and saved {args['configuration']}"


async def _call(
    db: DeviceBuilder, command: str, *, client: CollectingClient | None = None, **args: Any
) -> Any:
    """Invoke a WS command handler; *client* receives any stream frames."""
    handler = db.command_handlers.get(command)
    if handler is None:
        raise CommandError(ErrorCode.UNAVAILABLE, f"{command} is not available")
    return await handler(client=client or CollectingClient(), message_id=_MESSAGE_ID, **args)


def _only(args: dict[str, Any], *names: str) -> dict[str, Any]:
    """Return the *names* present in *args*: a tool forwards only what it names."""
    return {name: args[name] for name in names if name in args}


def _check_configuration(configuration: str | None, *, allow_secrets: bool) -> None:
    """Refuse a non-YAML name and, unless *allow_secrets*, the secrets file in any spelling."""
    if configuration is None:
        return
    if is_secrets_file(configuration):
        # Only the bare canonical name reads the file; an alias or a path is refused.
        if allow_secrets and configuration in SECRETS_FILENAMES:
            return
        raise CommandError(
            ErrorCode.INVALID_ARGS,
            "secrets.yaml is read with get_config and changed with set_secret",
        )
    if not is_device_config_name(configuration):
        raise CommandError(ErrorCode.INVALID_ARGS, "configuration must be a device .yaml filename")


def _prune(value: Any, *, include_advanced: bool = False) -> Any:
    """Drop empty values recursively; also hidden entries and, unless asked, advanced ones."""
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
    # Not ``False``: a default of false is a fact the model needs.
    return value is None or (isinstance(value, (str, list, dict)) and not value)
