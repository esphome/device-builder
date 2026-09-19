"""Tool registry, argument validation and ``tools/call`` result shaping."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple

from ..helpers.json import dumps_str

_LOGGER = logging.getLogger(__name__)

type ToolHandler[ContextT] = Callable[[ContextT, dict[str, Any]], Awaitable[Any]]
type ErrorTranslator = Callable[[Exception], McpToolError | None]

# Prefixes on a failed tool's text, so the model can tell the failure classes apart.
INVALID_ARGS = "invalid_args"
INTERNAL_ERROR = "internal_error"

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

    @property
    def definition(self) -> dict[str, Any]:
        """The ``tools/list`` entry."""
        return {"name": self.name, "description": self.description, "inputSchema": self.schema}


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
        """Register the decorated coroutine as tool *name*; every property type must be known."""
        properties = properties or {}
        for key, prop in properties.items():
            if prop.get("type") not in _JSON_TYPES:
                msg = f"Tool {name}: property {key} needs a type from {sorted(_JSON_TYPES)}"
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
        return [tool.definition for tool in self.values()]

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
                return _error(translated)
            _LOGGER.exception("MCP tool %s failed", name)
            return _error(McpToolError(INTERNAL_ERROR, f"Tool failed: {name}"))


def validate_args(schema: dict[str, Any], arguments: dict[str, Any]) -> None:
    """Check *arguments* against the top level of a tool's JSON Schema."""
    properties: dict[str, Any] = schema["properties"]
    for key in schema["required"]:
        if key not in arguments:
            raise McpToolError(INVALID_ARGS, f"Missing required argument: {key}")
    for key, value in arguments.items():
        if key not in properties:
            raise McpToolError(INVALID_ARGS, f"Unknown argument: {key}")
        json_type = properties[key]["type"]
        # A bool is only a boolean: JSON Schema keeps it out of integer and number.
        if isinstance(value, bool) != (json_type == "boolean") or not isinstance(
            value, _JSON_TYPES[json_type]
        ):
            raise McpToolError(INVALID_ARGS, f"Argument {key} must be {json_type}")


def _result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _error(err: McpToolError) -> dict[str, Any]:
    return _result(f"{err.code}: {err}", is_error=True)
