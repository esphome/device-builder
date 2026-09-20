"""Device Builder's MCP tools: each wraps a WS command and returns a text result."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ...constants import SECRETS_FILENAME, is_device_config_name, is_secrets_file
from ...controllers.automations.catalog import AUTOMATION_TYPES
from ...controllers.devices.helpers import scanned_component_entries
from ...controllers.firmware.follow import job_report
from ...helpers.ansi import plain_lines
from ...helpers.api import CollectingClient, CommandError
from ...mcp import INTERNAL_ERROR, McpToolError, ToolRegistry, closed_object
from ...models import ErrorCode

if TYPE_CHECKING:
    from ...device_builder import DeviceBuilder
    from ...mcp.tools import ToolHandler

_MESSAGE_ID = "mcp"
_LOGGER = logging.getLogger(__name__)


def _translate(err: Exception) -> McpToolError | None:
    """Map a user-facing WS command error onto ``McpToolError``; anything else is internal."""
    if isinstance(err, CommandError):
        if err.code is ErrorCode.INTERNAL_ERROR:
            _LOGGER.error("MCP tool hit a server fault: %s", err.message, exc_info=err)
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


_MAX_REFS = 50
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
    "Read a device's YAML configuration, or secrets.yaml (its values then enter this "
    "conversation).",
    {"configuration": _CONFIGURATION},
    ("configuration",),
    reads_secrets=True,
)
async def _get_config(db: DeviceBuilder, args: dict[str, Any]) -> Any:
    return await _call(db, "devices/get_config", **_only(args, "configuration"))


@_tool(
    "update_config",
    "Replace a device's YAML configuration with new content. Read it with get_config "
    "first, pass that text as expected, and change only what is needed; run "
    "validate_config or compile afterwards. Add or change an automation with "
    "upsert_automation instead. The previous text stays in the dashboard's version history.",
    {
        "configuration": _CONFIGURATION,
        "content": _prop("string", "The complete new YAML."),
        "expected": _prop(
            "string",
            "The text get_config returned, verbatim; the write is refused with "
            "precondition_failed if the file changed since, so nothing is overwritten unseen.",
        ),
    },
    ("configuration", "content", "expected"),
)
async def _update_config(db: DeviceBuilder, args: dict[str, Any]) -> str:
    await _call(db, "devices/update_config", **_only(args, "configuration", "content", "expected"))
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
        )
        | {"additionalProperties": True},
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
    await _call(
        db,
        "devices/validate",
        client=client,
        configuration=args["configuration"],
        show_secrets=False,
    )
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
    "queued for its next wake (deferred, upload_job_id null). A flash has no undo: the "
    "device runs whatever compiles, so read and validate the config first.",
    {
        "configuration": _CONFIGURATION,
        "port": _prop("string", "'OTA' (default), a serial port, or an IP/hostname."),
    },
    ("configuration",),
)
async def _install(db: DeviceBuilder, args: dict[str, Any]) -> dict[str, Any]:
    job = await _call(db, "firmware/install", **_only(args, "configuration", "port"))
    upload = None
    if (firmware := db.firmware) is not None:
        upload = next(firmware.state.dependents(job.job_id), None)
    if upload is None and not job.is_deferred_install:
        msg = f"install {job.job_id} was queued without its upload job"
        raise CommandError(ErrorCode.INTERNAL_ERROR, msg)
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
    job = await _call(db, "firmware/get_job", job_id=args["job_id"])
    if job is None:
        raise CommandError(ErrorCode.NOT_FOUND, f"Job not found: {args['job_id']}")
    return await job_report(job, tail_lines=args["tail_lines"])


@_tool(
    "cancel_job",
    "Cancel a queued or running firmware job. Returns the job's status right after the "
    "request: a queued job is cancelled at once, a running one is signalled and may still "
    "finish, so poll get_job for its final status. A null status means the job already "
    "left the retained history.",
    {"job_id": _JOB_ID},
    ("job_id",),
)
async def _cancel_job(db: DeviceBuilder, args: dict[str, Any]) -> dict[str, Any]:
    await _call(db, "firmware/cancel", **_only(args, "job_id"))
    job = await _call(db, "firmware/get_job", job_id=args["job_id"])
    return {"job_id": args["job_id"], "status": job.status if job else None}


@_tool(
    "search_components",
    "Search the ESPHome component catalog by name or keyword. total above the number of "
    "rows returned means the query was capped; narrow it.",
    {
        "query": _prop("string", "Search text."),
        "limit": _LIMIT,
    },
    ("query",),
)
async def _search_components(db: DeviceBuilder, args: dict[str, Any]) -> dict[str, Any]:
    response = await _call(
        db, "components/get_components", query=args["query"], limit=args["limit"]
    )
    return {
        "total": response.total,
        "components": [entry.to_dict() for entry in response.components],
    }


@_tool(
    "get_component",
    "Get a component's documentation: description, docs URL and every config field with "
    "type, description, required flag and allowed values. default_value is what ESPHome "
    "uses when the key is absent; an omitted flag is false. Advanced and YAML-only fields "
    "are omitted unless include_advanced is true.",
    {
        "component_id": _COMPONENT_ID,
        "platform": _prop(
            "string", "Target platform (esp32, esp8266, ...) to resolve platform defaults."
        ),
        "board_id": _prop(
            "string",
            "Board id from search_boards; resolves the chip variant's defaults, which "
            "platform alone cannot.",
        ),
        "include_advanced": _prop("boolean", "Include advanced and YAML-only fields.")
        | {"default": False},
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
        board_id=args.get("board_id"),
    )
    if component_id not in bodies:
        raise CommandError(ErrorCode.NOT_FOUND, f"Unknown component: {component_id}")
    return _visible(bodies[component_id].to_dict(), include_advanced=args["include_advanced"])


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
    return [entry.to_dict() for entry in entries]


@_tool(
    "search_boards",
    "Search the board catalog by name or chip; returns board ids for create_device. total "
    "above the number of rows returned means the query was capped; narrow it.",
    {
        "query": _prop("string", "Search text, e.g. 'esp32-c3' or 'nodemcu'."),
        "limit": _LIMIT,
    },
    ("query",),
)
async def _search_boards(db: DeviceBuilder, args: dict[str, Any]) -> dict[str, Any]:
    response = await _call(db, "boards/get_boards", query=args["query"], limit=args["limit"])
    return {"total": response.total, "boards": [b.to_dict() for b in response.boards]}


@_tool(
    "list_secret_names",
    "List the secret names defined in secrets.yaml. Reference one in YAML as '!secret <name>'.",
)
async def _list_secret_names(db: DeviceBuilder, _args: dict[str, Any]) -> Any:
    return await _call(db, "config/get_secrets")


@_tool(
    "set_secret",
    "Create one secret in secrets.yaml, the only way to change that file, or replace one "
    "with overwrite. Replacing is not recoverable: secrets.yaml is kept out of version "
    "history. Reference the secret in YAML as '!secret <name>'.",
    {
        "name": _prop("string", "Secret name, e.g. 'wifi_password'."),
        "value": _prop("string", "The secret value."),
        "overwrite": _prop("boolean", "Replace an existing value; the old one is lost.")
        | {"default": False},
    },
    ("name", "value"),
)
async def _set_secret(db: DeviceBuilder, args: dict[str, Any]) -> dict[str, Any]:
    result = await _call(
        db,
        "config/set_secret",
        key=args["name"],
        value=args["value"],
        overwrite=args["overwrite"],
    )
    if not result["created"] and not args["overwrite"]:
        msg = f"{args['name']} already exists; pass overwrite to replace it"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
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
    "on_* triggers, light effects) with their YAML, line range and location. Add or change "
    "one with upsert_automation; remove one with delete_automation.",
    {"configuration": _CONFIGURATION},
    ("configuration",),
)
async def _list_automations(db: DeviceBuilder, args: dict[str, Any]) -> Any:
    rows = await _call(db, "automations/parse", **_only(args, "configuration"))
    # The decomposed tree serves the visual editor; the model edits the YAML.
    return [{k: v for k, v in row.items() if k != "automation"} for row in rows]


@_tool(
    "get_available_automations",
    "The triggers, actions, conditions, scripts and component instances this device's "
    "config makes available for automations, by id. Call this before upsert_automation: "
    "ESPHome has no automation block; a trigger is an on_* key under esphome (device level) "
    "or under the component entry it belongs to, and scripts and intervals are top-level "
    "script and interval lists. An omitted list is empty.",
    {"configuration": _CONFIGURATION},
    ("configuration",),
)
async def _get_available_automations(db: DeviceBuilder, args: dict[str, Any]) -> Any:
    return await _call(db, "automations/get_available", **_only(args, "configuration"))


@_tool(
    "get_automation_docs",
    "Documentation for automation building blocks: the fields each trigger, action, condition, "
    "light effect or filter takes. An omitted flag is false; advanced and YAML-only fields are "
    "omitted unless include_advanced is true.",
    {
        "refs": _prop("array", "Building blocks to document.")
        | {
            "maxItems": _MAX_REFS,
            "items": closed_object(
                {
                    "type": _prop("string", "The building block kind.")
                    | {"enum": list(AUTOMATION_TYPES)},
                    "id": _prop(
                        "string", "Its id from get_available_automations, e.g. 'light.turn_on'."
                    )
                    | {"minLength": 1},
                },
                ("type", "id"),
            ),
        },
        "include_advanced": _prop("boolean", "Include advanced and YAML-only fields.")
        | {"default": False},
    },
    ("refs",),
)
async def _get_automation_docs(db: DeviceBuilder, args: dict[str, Any]) -> Any:
    keys = [f"{ref['type']}/{ref['id']}" for ref in args["refs"]]
    bodies = await _call(db, "automations/get_bodies", refs=args["refs"])
    if missing := [key for key in keys if key not in bodies]:
        raise CommandError(ErrorCode.NOT_FOUND, f"Unknown automation refs: {', '.join(missing)}")
    return _visible(bodies, include_advanced=args["include_advanced"])


@_tool(
    "upsert_automation",
    "Insert or replace one automation in a device config and save it; the backend renders "
    "the YAML in the right place. location kinds: {kind: 'device_on', trigger} for a device "
    "level on_* trigger; {kind: 'component_on', component_id, trigger} for a component "
    "instance's on_* trigger; {kind: 'script', id}; {kind: 'interval', index}; "
    "{kind: 'component_action', component_id, field} for an action-list field such as "
    "turn_on_action; {kind: 'light_effect', component_id, index}; {kind: 'api_action', "
    "action_name}. In a location, component_id is the instance id (devices[].id from "
    "get_available_automations, not the catalog type in devices[].component_id) and trigger "
    "is the bare YAML key such as on_press; add index only for a list-shaped handler (from "
    "list_automations); to append an interval or light_effect, pass index equal to the "
    "current list length. automation is {trigger_params, actions}: the location decides "
    "the trigger, so a trigger_id is ignored. trigger_params holds the block's own keys "
    "(a trigger's fields; for interval its interval period; for script its mode and "
    "parameters; for api_action its variables; for light_effect exactly one key, the "
    "effect id mapped to its params; for component_action nothing, it is ignored). Each "
    "action is {action_id, params, children, "
    "conditions}, children maps a branch name such as then or else to a list of actions, "
    "and each condition is {condition_id, params, children} with children a list of "
    "conditions; ids from get_available_automations, fields from get_automation_docs. An "
    "insert must not replace existing YAML; to replace, pass the automation's raw_yaml from "
    "list_automations as expected. The tool checks the shape, not the fields: esphome does "
    "that, so run validate_config after every write and repair what it reports by "
    "replacing the automation with expected.",
    {
        "configuration": _CONFIGURATION,
        "location": _prop("object", "Where the automation lives; see the description.")
        | {"additionalProperties": True},
        "automation": _prop("object", "The automation tree; see the description.")
        | {"additionalProperties": True},
        "expected": _prop(
            "string",
            "When replacing: the automation's raw_yaml exactly as list_automations returned it.",
        ),
    },
    ("configuration", "location", "automation"),
)
async def _upsert_automation(db: DeviceBuilder, args: dict[str, Any]) -> dict[str, Any]:
    result = await _call(
        db,
        "automations/upsert",
        save=True,
        **_only(args, "configuration", "location", "automation", "expected"),
    )
    return {"configuration": args["configuration"], "yaml_diff": result["yaml_diff"]}


@_tool(
    "delete_automation",
    "Remove one automation from a device config and save it; pass the location and raw_yaml "
    "from list_automations. A location is positional, so the delete is refused with "
    "precondition_failed if the automation changed or moved since it was listed.",
    {
        "configuration": _CONFIGURATION,
        "location": _prop("object", "The automation's location as returned by list_automations.")
        | {"additionalProperties": True},
        "expected": _prop(
            "string", "The automation's raw_yaml exactly as list_automations returned it."
        ),
    },
    ("configuration", "location", "expected"),
)
async def _delete_automation(db: DeviceBuilder, args: dict[str, Any]) -> dict[str, Any]:
    result = await _call(
        db,
        "automations/delete",
        save=True,
        **_only(args, "configuration", "location", "expected"),
    )
    return {"configuration": args["configuration"], "yaml_diff": result["yaml_diff"]}


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
        # Only the bare canonical name reads the file, the one set_secret writes.
        if allow_secrets and configuration == SECRETS_FILENAME:
            return
        raise CommandError(
            ErrorCode.INVALID_ARGS,
            "secrets.yaml is read with get_config and changed with set_secret",
        )
    if not is_device_config_name(configuration):
        raise CommandError(ErrorCode.INVALID_ARGS, "configuration must be a device .yaml filename")


def _visible(value: Any, *, include_advanced: bool) -> Any:
    """Drop advanced and hidden config entries recursively unless *include_advanced*."""
    if include_advanced:
        return value
    if isinstance(value, list):
        return [_visible(item, include_advanced=False) for item in value]
    if not isinstance(value, dict):
        return value
    if isinstance(value.get("config_entries"), list):
        value = value | {
            "config_entries": [
                entry
                for entry in value["config_entries"]
                if not (entry.get("hidden") or entry.get("advanced"))
            ]
        }
    return {k: _visible(v, include_advanced=False) for k, v in value.items()}
