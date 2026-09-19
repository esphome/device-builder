"""Tool registry, argument validation and ``tools/call`` result shaping."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple

from ..helpers.json import dumps_str

_LOGGER = logging.getLogger(__name__)

type ToolHandler[ContextT] = Callable[[ContextT, dict[str, Any]], Awaitable[Any]]
type ErrorTranslator = Callable[[Exception], McpToolError | None]

# Code words prefixed on a failed tool's text.
INVALID_ARGS = "invalid_args"
INTERNAL_ERROR = "internal_error"

# Schema keywords ``validate_args`` enforces.
_PROPERTY_KEYS = frozenset({"type", "description"})
_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "object": dict,
    "array": list,
}


class McpToolError(Exception):
    """A tool failure reported to the model as an ``isError`` result, prefixed by *code*."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class McpTool[ContextT](NamedTuple):
    """One tool: name, description, argument schema and the coroutine that runs it."""

    name: str
    description: str
    schema: dict[str, Any]
    handler: ToolHandler[ContextT]


class ToolRegistry[ContextT](dict[str, McpTool[ContextT]]):
    """Tools keyed by name; *translate* maps a handler's exceptions onto ``McpToolError``."""

    def __init__(self, translate: ErrorTranslator | None = None) -> None:
        super().__init__()
        self._translate: ErrorTranslator = translate or (lambda _err: None)

    def tool(
        self,
        name: str,
        description: str,
        properties: dict[str, dict[str, Any]] | None = None,
        required: tuple[str, ...] = (),
    ) -> Callable[[ToolHandler[ContextT]], ToolHandler[ContextT]]:
        """Register the decorated coroutine as tool *name*."""
        properties = properties or {}
        if name in self:
            msg = f"Tool {name} is already registered"
            raise ValueError(msg)
        for key, prop in properties.items():
            if not isinstance(prop.get("type"), str) or prop["type"] not in _JSON_TYPES:
                msg = f"Tool {name}: property {key} needs a type from {sorted(_JSON_TYPES)}"
                raise ValueError(msg)
            if unsupported := set(prop) - _PROPERTY_KEYS:
                msg = f"Tool {name}: property {key} has unenforced keywords {sorted(unsupported)}"
                raise ValueError(msg)
        if missing := set(required) - set(properties):
            msg = f"Tool {name}: required names not in properties: {sorted(missing)}"
            raise ValueError(msg)
        schema = {
            "type": "object",
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
        }

        def register(handler: ToolHandler[ContextT]) -> ToolHandler[ContextT]:
            self[name] = McpTool(name, description, schema, handler)
            return handler

        return register

    def definitions(self) -> list[dict[str, Any]]:
        """Build the ``tools/list`` payload."""
        return [
            {"name": t.name, "description": t.description, "inputSchema": t.schema}
            for t in self.values()
        ]

    async def call(self, context: ContextT, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run tool *name* and shape the outcome as a ``tools/call`` result."""
        tool = self[name]
        try:
            validate_args(tool.schema, arguments)
            value = await tool.handler(context, arguments)
            return _result(value if isinstance(value, str) else dumps_str(value))
        except McpToolError as err:
            return _error(err)
        except Exception as err:
            translated = self._translate(err)
            if translated is not None:
                _LOGGER.debug("MCP tool %s failed: %s: %s", name, translated.code, translated)
                return _error(translated)
            _LOGGER.exception("MCP tool %s failed", name)
            return _error(McpToolError(INTERNAL_ERROR, f"Tool failed: {name}"))


def validate_args(schema: dict[str, Any], arguments: dict[str, Any]) -> None:
    """Enforce a tool schema's ``required`` names and top-level ``type``s; nothing deeper."""
    properties: dict[str, Any] = schema["properties"]
    for key in schema["required"]:
        if key not in arguments:
            raise McpToolError(INVALID_ARGS, f"Missing required argument: {key}")
    for key, value in arguments.items():
        if key not in properties:
            raise McpToolError(INVALID_ARGS, f"Unknown argument: {key}")
        json_type = properties[key]["type"]
        # JSON Schema: a bool is not an integer or a number.
        if not isinstance(value, _JSON_TYPES[json_type]) or (
            isinstance(value, bool) and json_type != "boolean"
        ):
            raise McpToolError(INVALID_ARGS, f"Argument {key} must be {json_type}")


def _result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _error(err: McpToolError) -> dict[str, Any]:
    return _result(f"{err.code}: {err}", is_error=True)
