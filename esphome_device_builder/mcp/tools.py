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
        for key, spec in properties.items():
            if problem := _property_problem(spec, key, required=key in required, nested=False):
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

    Returns *arguments* with each absent property's ``default`` filled in and integral
    floats read back as the integers the schema declares.
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
    defaults = {key: spec["default"] for key, spec in properties.items() if "default" in spec}
    return defaults | {key: _normalised(properties[key], value) for key, value in arguments.items()}


def closed_object(
    properties: dict[str, Any], required: tuple[str, ...] = (), description: str | None = None
) -> dict[str, Any]:
    """Build an object schema that rejects keys outside *properties*."""
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }
    if description is not None:
        schema["description"] = description
    return schema


def open_object(description: str) -> dict[str, Any]:
    """Build an object schema that accepts any keys."""
    return prop("object", description, additionalProperties=True)


def any_value(description: str) -> dict[str, Any]:
    """Build a property schema that accepts a value of any type."""
    return {"description": description}


def prop(json_type: str, description: str, **keywords: Any) -> dict[str, Any]:
    """Build a property schema of *json_type*; *keywords* are JSON Schema keywords."""
    return {"type": json_type, "description": description, **keywords}


class _Keyword(NamedTuple):
    """One enforced keyword: the types it sits on, its registration check and its value check."""

    label: str
    types: frozenset[str]
    check: Callable[[dict[str, Any]], str | None]
    apply: Callable[[dict[str, Any], Any], str | None] | None = None
    noun: str | None = None


def _count(name: str) -> Callable[[dict[str, Any]], str | None]:
    def check(spec: dict[str, Any]) -> str | None:
        value = spec[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return f"needs a non-negative integer {name}"
        return None

    return check


def _bound(name: str) -> Callable[[dict[str, Any]], str | None]:
    def check(spec: dict[str, Any]) -> str | None:
        value = spec[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "needs numeric bounds"
        if name == "maximum" and "minimum" in spec and spec["minimum"] > value:
            return "has minimum above maximum"
        return None

    return check


def _enum_check(spec: dict[str, Any]) -> str | None:
    values = spec["enum"]
    if not isinstance(values, list) or not values or not all(isinstance(v, str) for v in values):
        return "needs a non-empty list of strings as its enum"
    if len(set(values)) != len(values):
        return "has duplicate enum values"
    return None


def _items_check(spec: dict[str, Any]) -> str | None:
    return None if isinstance(spec["items"], dict) else "needs a property schema as its items"


def _properties_check(spec: dict[str, Any]) -> str | None:
    if isinstance(spec["properties"], dict):
        return None
    return "needs a properties mapping; see closed_object"


def _items_apply(spec: dict[str, Any], values: Any) -> str | None:
    for index, item in enumerate(values):
        if problem := _value_problem(spec["items"], item):
            return f"a list whose item {index} is {problem}"
    return None


def _object_apply(spec: dict[str, Any], value: Any) -> str | None:
    for key in spec["required"]:
        if key not in value:
            return f"an object with {key}"
    for key, item in value.items():
        if key not in spec["properties"]:
            return f"an object without {key}"
        if problem := _value_problem(spec["properties"][key], item):
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
    "additionalProperties": _Keyword("object", frozenset({"object"}), lambda p: None),
}
# Keywords ``validate_args`` enforces; ``default`` only on a tool's own properties.
_PROPERTY_KEYS = frozenset(_KEYWORDS) | {"type", "description", "default"}
_NESTED_KEYS = _PROPERTY_KEYS - {"default"}


def _value_problem(spec: dict[str, Any], value: Any) -> str | None:
    """Return what *value* must be to satisfy *spec*, or None when it does."""
    if (json_type := spec.get("type")) is None:
        return None
    # JSON Schema: a bool is not an integer or a number, and an integral float is an integer.
    integral = json_type == "integer" and isinstance(value, float) and value.is_integer()
    if (not isinstance(value, _JSON_TYPES[json_type]) and not integral) or (
        isinstance(value, bool) and json_type != "boolean"
    ):
        return str(json_type)
    for name, keyword in _KEYWORDS.items():
        if name in spec and keyword.apply is not None and (problem := keyword.apply(spec, value)):
            return problem
    return None


def _normalised(spec: dict[str, Any], value: Any) -> Any:
    """Return *value* with integral floats read back as the integers the schema declares."""
    if spec.get("type") == "integer" and isinstance(value, float):
        return int(value)
    if "items" in spec:
        return [_normalised(spec["items"], item) for item in value]
    if "properties" in spec:
        return {key: _normalised(spec["properties"][key], item) for key, item in value.items()}
    return value


def _property_problem(
    spec: dict[str, Any], path: str, *, required: bool, nested: bool
) -> str | None:
    """Return why *spec* at *path* cannot be enforced, or None; nested shapes are checked too."""
    if problem := _own_problem(spec, required=required, nested=nested):
        return f"{path} {problem}"
    if "items" in spec:
        return _property_problem(spec["items"], f"{path}.items", required=False, nested=True)
    for key, sub in spec.get("properties", {}).items():
        if problem := _property_problem(
            sub, f"{path}.{key}", required=key in spec["required"], nested=True
        ):
            return problem
    return None


def _own_problem(spec: dict[str, Any], *, required: bool, nested: bool) -> str | None:
    """Return why *spec* itself cannot be enforced, or None; an ``any_value`` has nothing to."""
    keys = set(spec)
    if keys == {"description"}:
        return None
    if not isinstance(spec.get("type"), str) or spec["type"] not in _JSON_TYPES:
        return f"needs a type from {sorted(_JSON_TYPES)}, or a description alone (any_value)"
    if unsupported := keys - (_NESTED_KEYS if nested else _PROPERTY_KEYS):
        return f"has unenforced keywords {sorted(unsupported)}"
    return (
        _keyword_problem(spec)
        or _default_problem(spec, required=required)
        or _container_problem(spec)
    )


def _keyword_problem(spec: dict[str, Any]) -> str | None:
    """Return why one of *spec*'s enforced keywords cannot be honoured, or None."""
    for name, keyword in _KEYWORDS.items():
        if name not in spec:
            continue
        if spec["type"] not in keyword.types:
            return f"has {keyword.noun or name} on a non-{keyword.label} type"
        if problem := keyword.check(spec):
            return problem
    return None


def _container_problem(spec: dict[str, Any]) -> str | None:
    """Return why a container *spec*'s keywords do not add up to an enforceable shape, or None."""
    if spec["type"] == "array" and "items" not in spec:
        return "needs items" + (" beside maxItems" if "maxItems" in spec else "")
    if spec["type"] == "object":
        return _object_shape_problem(spec)
    return None


def _object_shape_problem(spec: dict[str, Any]) -> str | None:
    """Return why an object *spec* is neither a closed object nor a declared open one, or None."""
    if "properties" in spec:
        return _closed_object_problem(spec)
    if spec.get("additionalProperties") is not True:
        return "needs a shape: closed_object or open_object"
    if "required" in spec:
        return "has required names but no properties; see closed_object"
    return None


def _closed_object_problem(spec: dict[str, Any]) -> str | None:
    """Return why a *spec* with ``properties`` is not a well-formed closed object, or None."""
    if not isinstance(spec.get("required"), list):
        return "needs a required list beside properties; see closed_object"
    if len(set(spec["required"])) != len(spec["required"]):
        return "has duplicate required names"
    if missing := set(spec["required"]) - set(spec["properties"]):
        return f"has required names not in properties: {sorted(missing)}"
    if spec.get("additionalProperties") is not False:
        return "needs additionalProperties false beside properties; see closed_object"
    return None


def _default_problem(spec: dict[str, Any], *, required: bool) -> str | None:
    """Return why *spec*'s ``default`` cannot be honoured, or None."""
    if "default" not in spec:
        return None
    if required:
        return "is required, so its default is never used"
    if spec["type"] in _MUTABLE_TYPES:
        return "has a default on a mutable type, which every call would share"
    if problem := _value_problem(spec, spec["default"]):
        return f"needs a default that is {problem}"
    return None


def _result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _error(err: McpToolError) -> dict[str, Any]:
    return _result(f"{err.code}: {err}", is_error=True)
