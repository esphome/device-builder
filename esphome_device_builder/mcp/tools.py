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

_MUTABLE_TYPES = frozenset({"object", "array"})
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
            if problem := _property_problem(prop, key, required=key in required, nested=False):
                raise ValueError(f"Tool {name}: property {problem}")
        if missing := set(required) - set(properties):
            msg = f"Tool {name}: required names not in properties: {sorted(missing)}"
            raise ValueError(msg)
        if len(set(required)) != len(required):
            msg = f"Tool {name}: duplicate required names"
            raise ValueError(msg)
        schema = closed_object(properties, required)

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
    Enforce ``required`` names, ``type``s, ``enum``s, numeric bounds and item shapes.

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
    return defaults | {key: _normalised(properties[key], value) for key, value in arguments.items()}


def closed_object(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    """Build an object schema that rejects keys outside *properties*."""
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


class _Keyword(NamedTuple):
    """One enforced keyword: the types it sits on, its registration check and its value check."""

    label: str
    types: frozenset[str]
    check: Callable[[dict[str, Any]], str | None]
    apply: Callable[[dict[str, Any], Any], str | None] | None = None
    noun: str | None = None


def _count(name: str) -> Callable[[dict[str, Any]], str | None]:
    def check(prop: dict[str, Any]) -> str | None:
        value = prop[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return f"needs a non-negative integer {name}"
        if name == "maxItems" and "items" not in prop:
            return "needs items beside maxItems"
        return None

    return check


def _bound(name: str) -> Callable[[dict[str, Any]], str | None]:
    def check(prop: dict[str, Any]) -> str | None:
        value = prop[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "needs numeric bounds"
        if name == "maximum" and "minimum" in prop and prop["minimum"] > value:
            return "has minimum above maximum"
        return None

    return check


def _enum_check(prop: dict[str, Any]) -> str | None:
    values = prop["enum"]
    if not isinstance(values, list) or not values or not all(isinstance(v, str) for v in values):
        return "needs a non-empty list of strings as its enum"
    if len(set(values)) != len(values):
        return "has duplicate enum values"
    return None


def _items_check(prop: dict[str, Any]) -> str | None:
    return None if isinstance(prop["items"], dict) else "needs a property schema as its items"


def _properties_check(prop: dict[str, Any]) -> str | None:
    if (
        not isinstance(prop["properties"], dict)
        or not isinstance(prop.get("required"), list)
        or prop.get("additionalProperties") is not False
    ):
        return "needs properties, required and additionalProperties false; see closed_object"
    if len(set(prop["required"])) != len(prop["required"]):
        return "has duplicate required names"
    if missing := set(prop["required"]) - set(prop["properties"]):
        return f"has required names not in properties: {sorted(missing)}"
    return None


def _openness_check(prop: dict[str, Any]) -> str | None:
    if "properties" in prop:
        return None
    if prop["additionalProperties"] is not True:
        return "needs additionalProperties true for an open object; see closed_object"
    return None


def _items_apply(prop: dict[str, Any], values: Any) -> str | None:
    for index, item in enumerate(values):
        if problem := _value_problem(prop["items"], item):
            return f"a list whose item {index} is {problem}"
    return None


def _object_apply(prop: dict[str, Any], value: Any) -> str | None:
    for key in prop["required"]:
        if key not in value:
            return f"an object with {key}"
    for key, item in value.items():
        if key not in prop["properties"]:
            return f"an object without {key}"
        if problem := _value_problem(prop["properties"][key], item):
            return f"an object whose {key} is {problem}"
    return None


_NUMERIC = frozenset({"integer", "number"})
# Every keyword ``validate_args`` enforces, in the order their value checks run.
_KEYWORDS: dict[str, _Keyword] = {
    "minimum": _Keyword(
        "numeric",
        _NUMERIC,
        _bound("minimum"),
        lambda p, v: f"at least {p['minimum']}" if v < p["minimum"] else None,
        noun="bounds",
    ),
    "maximum": _Keyword(
        "numeric",
        _NUMERIC,
        _bound("maximum"),
        lambda p, v: f"at most {p['maximum']}" if v > p["maximum"] else None,
        noun="bounds",
    ),
    "enum": _Keyword(
        "string",
        frozenset({"string"}),
        _enum_check,
        lambda p, v: f"one of {p['enum']}" if v not in p["enum"] else None,
    ),
    "minLength": _Keyword(
        "string",
        frozenset({"string"}),
        _count("minLength"),
        lambda p, v: (
            f"at least {p['minLength']} characters long" if len(v) < p["minLength"] else None
        ),
    ),
    "maxItems": _Keyword(
        "array",
        frozenset({"array"}),
        _count("maxItems"),
        lambda p, v: f"a list of at most {p['maxItems']} items" if len(v) > p["maxItems"] else None,
    ),
    "items": _Keyword("array", frozenset({"array"}), _items_check, _items_apply),
    "properties": _Keyword("object", frozenset({"object"}), _properties_check, _object_apply),
    "required": _Keyword("object", frozenset({"object"}), lambda p: None),
    "additionalProperties": _Keyword("object", frozenset({"object"}), _openness_check),
}
# Keywords ``validate_args`` enforces; ``default`` only on a tool's own properties.
_PROPERTY_KEYS = frozenset(_KEYWORDS) | {"type", "description", "default"}
_NESTED_KEYS = _PROPERTY_KEYS - {"default"}


def _value_problem(prop: dict[str, Any], value: Any) -> str | None:
    """Return what *value* must be to satisfy *prop*, or None when it does."""
    json_type = prop["type"]
    # JSON Schema: a bool is not an integer or a number, and an integral float is an integer.
    integral = json_type == "integer" and isinstance(value, float) and value.is_integer()
    if (not isinstance(value, _JSON_TYPES[json_type]) and not integral) or (
        isinstance(value, bool) and json_type != "boolean"
    ):
        return str(json_type)
    for name, keyword in _KEYWORDS.items():
        if name in prop and keyword.apply is not None and (problem := keyword.apply(prop, value)):
            return problem
    return None


def _normalised(prop: dict[str, Any], value: Any) -> Any:
    """Return *value* with integral floats read back as the integers the schema declares."""
    if prop["type"] == "integer" and isinstance(value, float):
        return int(value)
    if "items" in prop:
        return [_normalised(prop["items"], item) for item in value]
    if "properties" in prop:
        return {key: _normalised(prop["properties"][key], item) for key, item in value.items()}
    return value


def _property_problem(
    prop: dict[str, Any], path: str, *, required: bool, nested: bool
) -> str | None:
    """Return why *prop* at *path* cannot be enforced, or None; nested shapes are checked too."""
    if not isinstance(prop.get("type"), str) or prop["type"] not in _JSON_TYPES:
        return f"{path} needs a type from {sorted(_JSON_TYPES)}"
    if unsupported := set(prop) - (_NESTED_KEYS if nested else _PROPERTY_KEYS):
        return f"{path} has unenforced keywords {sorted(unsupported)}"
    if problem := (
        _keyword_problem(prop)
        or _default_problem(prop, required=required)
        or _container_problem(prop)
    ):
        return f"{path} {problem}"
    if "items" in prop:
        return _property_problem(prop["items"], f"{path}.items", required=False, nested=True)
    for key, sub in prop.get("properties", {}).items():
        if problem := _property_problem(
            sub, f"{path}.{key}", required=key in prop["required"], nested=True
        ):
            return problem
    return None


def _keyword_problem(prop: dict[str, Any]) -> str | None:
    """Return why one of *prop*'s enforced keywords cannot be honoured, or None."""
    for name, keyword in _KEYWORDS.items():
        if name not in prop:
            continue
        if prop["type"] not in keyword.types:
            return f"has {keyword.noun or name} on a non-{keyword.label} type"
        if problem := keyword.check(prop):
            return problem
    return None


def _container_problem(prop: dict[str, Any]) -> str | None:
    """Return why a container *prop* declares no shape, or None; an open object says so."""
    if prop["type"] == "array" and "items" not in prop:
        return "needs items"
    if prop["type"] == "object" and "additionalProperties" not in prop:
        return "needs a shape: closed_object, or additionalProperties true for an open object"
    return None


def _default_problem(prop: dict[str, Any], *, required: bool) -> str | None:
    """Return why *prop*'s ``default`` cannot be honoured, or None."""
    if "default" not in prop:
        return None
    if required:
        return "is required, so its default is never used"
    if prop["type"] in _MUTABLE_TYPES:
        return "has a default on a mutable type, which every call would share"
    if problem := _value_problem(prop, prop["default"]):
        return f"needs a default that is {problem}"
    return None


def _result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _error(err: McpToolError) -> dict[str, Any]:
    return _result(f"{err.code}: {err}", is_error=True)
