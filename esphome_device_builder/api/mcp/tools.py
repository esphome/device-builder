"""Device Builder's MCP tools: each wraps a WS command and returns a text result."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from esphome.const import SECRETS_FILES

from ...controllers.automations.catalog import AUTOMATION_TYPES
from ...controllers.devices.helpers import raise_device_not_found, require_catalog
from ...controllers.firmware.follow import initial_snapshot
from ...controllers.firmware.persistence import job_dict_without_output
from ...helpers.ansi import ANSI_CSI_RE
from ...helpers.api import CollectingClient, CommandError
from ...helpers.async_ import run_in_executor
from ...helpers.device_yaml import ESPHOME_CONFIG_TIMEOUT
from ...helpers.secrets_state import validate_secrets_content
from ...helpers.yaml import apply_yaml_diff
from ...mcp import INTERNAL_ERROR, McpToolError, ToolRegistry
from ...models import ErrorCode

if TYPE_CHECKING:
    from ...device_builder import DeviceBuilder

    type ToolHandler = Callable[[DeviceBuilder, dict[str, Any]], Any]

_MESSAGE_ID = "mcp"
# No NTFS stream suffix (``::$DATA``) and no 8.3 alias (``SECRET~1.YAM``).
_CONFIG_NAME_RE = re.compile(r"[^:~]+\.ya?ml")
_LOGGER = logging.getLogger(__name__)

_DEFAULT_TAIL_LINES = 50
_MAX_TAIL_LINES = 1000
_MAX_SEARCH_RESULTS = 100
_MIN_REDACTED_SECRET_LEN = 6


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
) -> Callable[[ToolHandler], ToolHandler]:
    """Register a tool whose ``configuration`` argument, if any, may not name the secrets file."""

    def register(handler: ToolHandler) -> ToolHandler:
        async def guarded(db: DeviceBuilder, args: dict[str, Any]) -> Any:
            _refuse_secrets(args.get("configuration"))
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
_TAIL_LINES = _prop(
    "integer", f"Output lines to keep from the end (default 50, max {_MAX_TAIL_LINES})."
)


async def _call(
    db: DeviceBuilder, command: str, *, client: CollectingClient | None = None, **args: Any
) -> Any:
    """Invoke a WS command handler; *client* receives any stream frames."""
    handler = db.command_handlers.get(command)
    if handler is None:
        raise CommandError(ErrorCode.UNAVAILABLE, f"{command} is not available")
    return await handler(
        client=client or CollectingClient(tail=_DEFAULT_TAIL_LINES), message_id=_MESSAGE_ID, **args
    )


def _refuse_secrets(configuration: Any) -> None:
    """Refuse the secrets file in any spelling: its contents never reach a model."""
    if configuration is None:
        return
    if not isinstance(configuration, str):
        raise CommandError(ErrorCode.INVALID_ARGS, "configuration must be a string")
    name = Path(configuration).name.rstrip(". ").lower()
    if name in SECRETS_FILES:
        raise CommandError(ErrorCode.INVALID_ARGS, "secrets.yaml is not available over MCP")
    if not _CONFIG_NAME_RE.fullmatch(name):
        raise CommandError(ErrorCode.INVALID_ARGS, "configuration must name a .yaml file")


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
    return value is None or value is False or (isinstance(value, (str, list, dict)) and not value)


def _bounded(args: dict[str, Any], key: str, default: int, minimum: int, maximum: int) -> int:
    """Return integer argument *key*: below *minimum* is invalid, above *maximum* clamps."""
    value: int = args.get(key, default)
    if value < minimum:
        raise CommandError(ErrorCode.INVALID_ARGS, f"{key} must be at least {minimum}")
    return min(value, maximum)


def _tail_lines(args: dict[str, Any]) -> int:
    return _bounded(args, "tail_lines", _DEFAULT_TAIL_LINES, 0, _MAX_TAIL_LINES)


def _search_limit(args: dict[str, Any]) -> int:
    return _bounded(args, "limit", 20, 1, _MAX_SEARCH_RESULTS)


def _load_secrets(config_dir: Path) -> dict[Any, Any]:
    """Return the union of every secrets file spelling; raise when one exists but is unreadable."""
    secrets: dict[Any, Any] = {}
    for filename in SECRETS_FILES:
        path = config_dir / filename
        try:
            content = path.read_text("utf-8")
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as err:
            _LOGGER.warning(
                "%s could not be read; withholding validate output", filename, exc_info=err
            )
            msg = f"{filename} could not be read; validation output withheld"
            raise CommandError(ErrorCode.UNAVAILABLE, msg) from err
        try:
            secrets |= validate_secrets_content(content, path)
        except ValueError as err:
            _LOGGER.warning(
                "%s could not be parsed; withholding validate output", filename, exc_info=err
            )
            msg = f"{filename} could not be parsed; validation output withheld"
            raise CommandError(ErrorCode.UNAVAILABLE, msg) from err
    return secrets


def _scalar_leaves(value: Any) -> list[str]:
    """Return every scalar inside *value* as text, walking lists and mappings."""
    if isinstance(value, dict):
        return [leaf for item in value.values() for leaf in _scalar_leaves(item)]
    if isinstance(value, list):
        return [leaf for item in value for leaf in _scalar_leaves(item)]
    if value is None or isinstance(value, bool):
        return []
    return [str(value)]


def _redact_secret_values(lines: list[str], secrets: dict[Any, Any]) -> list[str]:
    """Replace every secrets value of credential length in *lines* with ``<removed>``."""
    values = {text for text in _scalar_leaves(secrets) if len(text) >= _MIN_REDACTED_SECRET_LEN}
    for value in sorted(values, key=len, reverse=True):
        lines = [line.replace(value, "<removed>") for line in lines]
    return lines


def _strip_lines(lines: list[str]) -> list[str]:
    """Output lines with ANSI colour and line terminators removed."""
    return [ANSI_CSI_RE.sub("", line).rstrip("\r\n") for line in lines]


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
    "Read a device's YAML configuration.",
    {"configuration": _CONFIGURATION},
    ("configuration",),
)
async def _get_config(db: DeviceBuilder, args: dict[str, Any]) -> Any:
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
    "Validate a device config with esphome and return the last output lines (truncated "
    "says whether earlier lines were dropped; secret values are removed). Bounded to one "
    "minute; a timed out run reports timed_out.",
    {"configuration": _CONFIGURATION, "tail_lines": _TAIL_LINES},
    ("configuration",),
)
async def _validate_config(db: DeviceBuilder, args: dict[str, Any]) -> dict[str, Any]:
    client = CollectingClient(tail=_tail_lines(args))
    # Under asyncio.timeout the stream helper re-raises the cancel (TimeoutError below);
    # a handler that swallows it instead still reports through the expired deadline.
    deadline = asyncio.timeout(ESPHOME_CONFIG_TIMEOUT)
    try:
        async with deadline:
            await _call(db, "devices/validate", client=client, configuration=args["configuration"])
    except TimeoutError:
        if not deadline.expired():
            raise
    secrets = await run_in_executor(_load_secrets, db.settings.config_dir)
    output = _redact_secret_values(_strip_lines(list(client.output)), secrets)
    if deadline.expired():
        return {
            "success": False,
            "timed_out": True,
            "output": output,
            "truncated": client.truncated,
        }
    result = client.result
    if result is None or "success" not in result or "code" not in result:
        _LOGGER.error(
            "MCP validate of %s produced no result frame: %r", args["configuration"], result
        )
        raise McpToolError(INTERNAL_ERROR, "Validation produced no result")
    return {
        "success": result["success"],
        "exit_code": result["code"],
        "output": output,
        "truncated": client.truncated,
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
    tail_lines = _tail_lines(args)
    job = await _call(db, "firmware/get_job", job_id=args["job_id"])
    if job is None:
        raise CommandError(ErrorCode.NOT_FOUND, f"Job not found: {args['job_id']}")
    output = (await initial_snapshot(job, job.job_id))[-tail_lines:] if tail_lines > 0 else []
    return job_dict_without_output(job) | {
        "queued_update_armed": job.is_queued_update_armed,
        "output": _strip_lines(output),
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
    limit = _search_limit(args)
    response = await _call(db, "components/get_components", **(args | {"limit": limit}))
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
    "List the catalog components a device config used as of its last scan (every save "
    "rescans), with a short description and docs URL for each. Call get_component for "
    "the fields of any of them.",
    {"configuration": _CONFIGURATION},
    ("configuration",),
)
async def _get_config_components(db: DeviceBuilder, args: dict[str, Any]) -> list[dict[str, Any]]:
    catalog = require_catalog(db)
    if db.devices is None:
        raise CommandError(ErrorCode.UNAVAILABLE, "Devices are not loaded")
    configuration = args["configuration"]
    if (device := db.devices.get_by_configuration(configuration)) is None:
        raise_device_not_found(configuration)
    # A resolved config always carries ``esphome``; an empty list means the scan could not load it.
    if not device.component_ids:
        msg = f"{configuration} has not been resolved since its last change; run validate_config"
        raise CommandError(ErrorCode.UNAVAILABLE, msg)
    # Only catalog ids are echoed: a resolved ``platform:`` value may be a ``!secret``.
    entries = (catalog.index_entry(cid) for cid in device.component_ids)
    return [_prune(entry.to_dict()) for entry in entries if entry is not None]


@_tool(
    "search_boards",
    "Search the board catalog by name or chip; returns board ids for create_device.",
    {
        "query": _prop("string", "Search text, e.g. 'esp32-c3' or 'nodemcu'."),
        "limit": _prop("integer", f"Max results (default 20, max {_MAX_SEARCH_RESULTS})."),
    },
    ("query",),
)
async def _search_boards(db: DeviceBuilder, args: dict[str, Any]) -> list[dict[str, Any]]:
    limit = _search_limit(args)
    response = await _call(db, "boards/get_boards", **(args | {"limit": limit}))
    return [_prune(board.to_dict()) for board in response.boards]


@_tool(
    "list_secret_names",
    "List the secret names defined in secrets.yaml, never the values. Reference one in "
    "YAML as '!secret <name>'.",
)
async def _list_secret_names(db: DeviceBuilder, _args: dict[str, Any]) -> Any:
    return await _call(db, "config/get_secrets")


@_tool(
    "set_secret",
    "Create or update one secret in secrets.yaml from a value the user supplied. The value "
    "is never read back; reference it in YAML as '!secret <name>'.",
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
    "credentials are never passed inline. Returns the new configuration filename.",
    {
        "name": _prop("string", "Device name (its hostname), e.g. 'living-room-sensor'."),
        "friendly_name": _prop("string", "Human readable name."),
        "board_id": _prop("string", "Board id from search_boards, e.g. 'esp32dev'."),
    },
    ("name",),
)
async def _create_device(db: DeviceBuilder, args: dict[str, Any]) -> Any:
    response = await _call(db, "devices/create", **args)
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
    rows = await _call(db, "automations/parse", **args)
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
    return _prune(await _call(db, "automations/get_available", **args))


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
    include_advanced = args.pop("include_advanced", False)
    for ref in args["refs"]:
        if (
            not isinstance(ref, dict)
            or ref.get("type") not in AUTOMATION_TYPES
            or not isinstance(ref.get("id"), str)
            or not ref["id"]
        ):
            msg = f"each ref needs a type of {', '.join(AUTOMATION_TYPES)} and an id"
            raise CommandError(ErrorCode.INVALID_ARGS, msg)
    bodies = await _call(db, "automations/get_bodies", **args)
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
    configuration = args["configuration"]
    text = await _call(db, "devices/get_config", configuration=configuration)
    splice = await _call(db, "automations/delete", yaml=text, **args)
    new_text = apply_yaml_diff(text, splice["yaml_diff"])
    if new_text == text:
        _LOGGER.error("MCP delete_automation left %s unchanged: %r", configuration, splice)
        raise McpToolError(INTERNAL_ERROR, "Delete produced no change")
    await _call(db, "devices/update_config", configuration=configuration, content=new_text)
    return f"Removed the automation and saved {configuration}"
