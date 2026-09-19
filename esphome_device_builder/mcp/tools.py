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
_PROPERTY_KEYS = frozenset({"type", "description", "minimum", "maximum", "default"})
_NUMERIC_TYPES = frozenset({"integer", "number"})
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
            problem = _bounds_problem(prop) or _default_problem(prop, required=key in required)
            if problem:
                raise ValueError(f"Tool {name}: property {key} {problem}")
        if missing := set(required) - set(properties):
            msg = f"Tool {name}: required names not in properties: {sorted(missing)}"
            raise ValueError(msg)
        if len(set(required)) != len(required):
            msg = f"Tool {name}: duplicate required names"
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
            value = await tool.handler(context, validate_args(tool.schema, arguments))
        except McpToolError as err:
            return _error(err)
        except Exception as err:
            translated = self._translate_safely(err)
            if translated is not None:
                _LOGGER.debug("MCP tool %s failed: %s", name, translated, exc_info=err)
                return _error(translated)
            _LOGGER.exception("MCP tool %s failed", name)
            return _error(McpToolError(INTERNAL_ERROR, f"Tool failed: {name}"))
        else:
            # The tool ran, so the model must not retry it; only the reply is unusable.
            try:
                return _result(value if isinstance(value, str) else dumps_str(value))
            except Exception:
                _LOGGER.exception("MCP tool %s returned an unserialisable result", name)
                msg = f"Tool {name} ran but its result could not be serialised; do not retry"
                return _error(McpToolError(INTERNAL_ERROR, msg))

    def _translate_safely(self, err: Exception) -> McpToolError | None:
        """Run the translator; a translator that itself raises counts as untranslated."""
        try:
            return self._translate(err)
        except Exception:
            _LOGGER.exception("MCP error translator failed")
            return None


def validate_args(schema: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    """
    Enforce ``required`` names, top-level ``type``s and numeric bounds; nothing deeper.

    Returns *arguments* with each absent property's ``default`` filled in.
    """
    properties: dict[str, Any] = schema["properties"]
    for key in schema["required"]:
        if key not in arguments:
            raise McpToolError(INVALID_ARGS, f"Missing required argument: {key}")
    for key, value in arguments.items():
        if key not in properties:
            raise McpToolError(INVALID_ARGS, f"Unknown argument: {key}")
        if problem := _value_problem(properties[key], value):
            raise McpToolError(INVALID_ARGS, f"Argument {key} must be {problem}")
    defaults = {key: prop["default"] for key, prop in properties.items() if "default" in prop}
    return defaults | arguments


def _value_problem(prop: dict[str, Any], value: Any) -> str | None:
    """Return what *value* must be to satisfy *prop*, or None when it does."""
    json_type = prop["type"]
    # JSON Schema: a bool is not an integer or a number.
    if not isinstance(value, _JSON_TYPES[json_type]) or (
        isinstance(value, bool) and json_type != "boolean"
    ):
        return str(json_type)
    if "minimum" in prop and value < prop["minimum"]:
        return f"at least {prop['minimum']}"
    if "maximum" in prop and value > prop["maximum"]:
        return f"at most {prop['maximum']}"
    return None


def _bounds_problem(prop: dict[str, Any]) -> str | None:
    """Return why *prop*'s ``minimum`` / ``maximum`` cannot be enforced, or None."""
    bounds = [prop[k] for k in ("minimum", "maximum") if k in prop]
    if bounds and prop["type"] not in _NUMERIC_TYPES:
        return "has bounds on a non-numeric type"
    if any(isinstance(b, bool) or not isinstance(b, (int, float)) for b in bounds):
        return "needs numeric bounds"
    if len(bounds) == 2 and bounds[0] > bounds[1]:
        return "has minimum above maximum"
    return None


def _default_problem(prop: dict[str, Any], *, required: bool) -> str | None:
    """Return why *prop*'s ``default`` cannot be honoured, or None."""
    if "default" not in prop:
        return None
    if required:
        return "is required, so its default is never used"
    if problem := _value_problem(prop, prop["default"]):
        return f"needs a default that is {problem}"
    return None


def _result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _error(err: McpToolError) -> dict[str, Any]:
    return _result(f"{err.code}: {err}", is_error=True)
