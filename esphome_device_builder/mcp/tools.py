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
        self.message = message


class McpTool[ContextT](NamedTuple):
    """One tool: its ``tools/list`` definition plus the coroutine that runs it."""

    definition: dict[str, Any]
    handler: ToolHandler[ContextT]


class ToolRegistry[ContextT](dict[str, McpTool[ContextT]]):
    """Tools keyed by name; *translate* maps a handler's exceptions onto ``McpToolError``."""

    def __init__(self, translate: ErrorTranslator | None = None) -> None:
        super().__init__()
        self._translate = translate

    def tool(
        self,
        name: str,
        description: str,
        properties: dict[str, dict[str, Any]] | None = None,
        required: tuple[str, ...] = (),
    ) -> Callable[[ToolHandler[ContextT]], ToolHandler[ContextT]]:
        """Register the decorated coroutine as tool *name* with the given argument schema."""

        def register(handler: ToolHandler[ContextT]) -> ToolHandler[ContextT]:
            schema = {
                "type": "object",
                "properties": properties or {},
                "required": list(required),
                "additionalProperties": False,
            }
            self[name] = McpTool(
                {"name": name, "description": description, "inputSchema": schema}, handler
            )
            return handler

        return register

    def definitions(self) -> list[dict[str, Any]]:
        """Build the ``tools/list`` payload."""
        return [tool.definition for tool in self.values()]

    async def call(self, context: ContextT, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run tool *name* and shape the outcome as a ``tools/call`` result."""
        tool = self[name]
        try:
            validate_args(tool.definition["inputSchema"], arguments)
            value = await tool.handler(context, arguments)
            return _result(value if isinstance(value, str) else dumps_str(value))
        except McpToolError as err:
            return _error(err)
        except Exception as err:
            translated = self._translate(err) if self._translate else None
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
        expected = _JSON_TYPES[json_type]
        # ``bool`` is an ``int`` subclass; an integer slot must not accept it.
        if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
            raise McpToolError(INVALID_ARGS, f"Argument {key} must be {json_type}")


def _result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _error(err: McpToolError) -> dict[str, Any]:
    return _result(f"{err.code}: {err.message}", is_error=True)
