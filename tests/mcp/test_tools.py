"""Tool registry: registration, argument validation, error translation and result shaping."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import jsonschema
import pytest

from esphome_device_builder.mcp import INTERNAL_ERROR, INVALID_ARGS, McpToolError, ToolRegistry
from esphome_device_builder.mcp.tools import validate_args

_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "count": {"type": "integer"},
        "ratio": {"type": "number"},
        "flag": {"type": "boolean"},
        "fields": {"type": "object"},
        "items": {"type": "array"},
    },
    "required": ["name"],
    "additionalProperties": False,
}


def test_registered_schema_is_valid_json_schema() -> None:
    tools: ToolRegistry[None] = ToolRegistry()

    @tools.tool("t", "desc", {"a": {"type": "string", "description": "x"}}, ("a",))
    async def _t(_context: None, _args: dict[str, Any]) -> str:
        return "ok"

    (definition,) = tools.definitions()
    jsonschema.Draft202012Validator.check_schema(definition["inputSchema"])


@pytest.mark.parametrize(
    "prop",
    [{}, {"type": "enum"}, {"oneOf": []}, {"type": ["string", "null"]}],
    ids=["none", "enum", "oneOf", "type_list"],
)
def test_registration_rejects_types_the_validator_cannot_check(prop: dict[str, Any]) -> None:
    tools: ToolRegistry[None] = ToolRegistry()
    with pytest.raises(ValueError, match="property a needs a type"):
        tools.tool("t", "desc", {"a": prop})


@pytest.mark.parametrize(
    "arguments",
    [
        {"name": "x"},
        {"name": "x", "count": 0, "ratio": 1.5, "flag": False, "fields": {}, "items": []},
        {"name": "x", "ratio": 2},
    ],
)
def test_validate_args_accepts(arguments: dict[str, Any]) -> None:
    validate_args(_SCHEMA, arguments)


@pytest.mark.parametrize(
    ("arguments", "fragment"),
    [
        pytest.param({}, "Missing required argument: name", id="missing"),
        pytest.param({"name": "x", "extra": 1}, "Unknown argument: extra", id="unknown"),
        pytest.param({"name": 5}, "name must be string", id="wrong_type"),
        pytest.param({"name": "x", "count": True}, "count must be integer", id="bool_as_int"),
        pytest.param({"name": "x", "ratio": True}, "ratio must be number", id="bool_as_number"),
        pytest.param({"name": "x", "count": 1.5}, "count must be integer", id="float_as_int"),
        pytest.param({"name": "x", "flag": 1}, "flag must be boolean", id="int_as_bool"),
    ],
)
def test_validate_args_rejects(arguments: dict[str, Any], fragment: str) -> None:
    with pytest.raises(McpToolError) as excinfo:
        validate_args(_SCHEMA, arguments)
    assert excinfo.value.code == INVALID_ARGS
    assert fragment in str(excinfo.value)


@pytest.fixture
def tools() -> ToolRegistry[None]:
    def translate(err: Exception) -> McpToolError | None:
        return McpToolError("busy", str(err)) if isinstance(err, BlockingIOError) else None

    registry: ToolRegistry[None] = ToolRegistry(translate=translate)

    @registry.tool("text", "")
    async def _text(_context: None, _args: dict[str, Any]) -> str:
        return "plain"

    @registry.tool("data", "")
    async def _data(_context: None, _args: dict[str, Any]) -> dict[str, Any]:
        return {"a": 1}

    @registry.tool("fail", "")
    async def _fail(_context: None, _args: dict[str, Any]) -> None:
        raise McpToolError("not_found", "gone")

    @registry.tool("busy", "")
    async def _busy(_context: None, _args: dict[str, Any]) -> None:
        raise BlockingIOError("try later")

    @registry.tool("crash", "")
    async def _crash(_context: None, _args: dict[str, Any]) -> None:
        raise RuntimeError("boom")

    @registry.tool("weird", "")
    async def _weird(_context: None, _args: dict[str, Any]) -> object:
        return object()

    return registry


_UNSERIALISABLE = "Tool weird ran but its result could not be serialised; do not retry"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        pytest.param("text", (False, "plain"), id="string_result"),
        pytest.param("data", (False, '{"a":1}'), id="json_result"),
        pytest.param("fail", (True, "not_found: gone"), id="tool_error"),
        pytest.param("busy", (True, "busy: try later"), id="translated"),
        pytest.param("crash", (True, f"{INTERNAL_ERROR}: Tool failed: crash"), id="untranslated"),
        pytest.param("weird", (True, f"{INTERNAL_ERROR}: {_UNSERIALISABLE}"), id="unserialisable"),
    ],
)
async def test_call_shapes_results_and_errors(
    tools: ToolRegistry[None], name: str, expected: tuple[bool, str]
) -> None:
    result = await tools.call(None, name, {})
    assert (result["isError"], result["content"][0]["text"]) == expected
    assert result["content"][0]["type"] == "text"


async def test_a_raising_translator_counts_as_untranslated() -> None:
    def translate(_err: Exception) -> McpToolError | None:
        raise RuntimeError("translator bug")

    tools: ToolRegistry[None] = ToolRegistry(translate=translate)

    @tools.tool("crash", "")
    async def _crash(_context: None, _args: dict[str, Any]) -> None:
        raise ValueError("boom")

    result = await tools.call(None, "crash", {})
    assert result == {
        "content": [{"type": "text", "text": f"{INTERNAL_ERROR}: Tool failed: crash"}],
        "isError": True,
    }


def test_registration_rejects_keywords_the_validator_does_not_enforce() -> None:
    tools: ToolRegistry[None] = ToolRegistry()
    with pytest.raises(ValueError, match=r"unenforced keywords \['enum'\]"):
        tools.tool("t", "desc", {"a": {"type": "string", "enum": ["x"]}})


_BOUNDED = {"type": "integer", "minimum": 1, "maximum": 10, "default": 5}


@pytest.mark.parametrize(
    ("prop", "fragment"),
    [
        pytest.param({"type": "string", "minimum": 1}, "bounds on a non-numeric type", id="string"),
        pytest.param({"type": "integer", "minimum": "1"}, "needs numeric bounds", id="text_bound"),
        pytest.param({"type": "integer", "maximum": True}, "needs numeric bounds", id="bool_bound"),
        pytest.param(
            {"type": "integer", "minimum": 5, "maximum": 1}, "minimum above maximum", id="inverted"
        ),
        pytest.param(_BOUNDED | {"default": 11}, "default that is at most 10", id="default_high"),
        pytest.param({"type": "integer", "default": "5"}, "default that is integer", id="type"),
    ],
)
def test_registration_rejects_bounds_and_defaults_it_cannot_honour(
    prop: dict[str, Any], fragment: str
) -> None:
    tools: ToolRegistry[None] = ToolRegistry()
    with pytest.raises(ValueError, match=f"property a .*{fragment}"):
        tools.tool("t", "desc", {"a": prop})


def test_registration_rejects_a_default_on_a_required_property() -> None:
    tools: ToolRegistry[None] = ToolRegistry()
    with pytest.raises(ValueError, match="is required, so its default is never used"):
        tools.tool("t", "desc", {"a": _BOUNDED}, ("a",))


@pytest.mark.parametrize(
    ("arguments", "fragment"),
    [({"limit": 0}, "limit must be at least 1"), ({"limit": 11}, "limit must be at most 10")],
    ids=["below", "above"],
)
async def test_call_refuses_a_value_outside_its_bounds(
    arguments: dict[str, Any], fragment: str
) -> None:
    tools: ToolRegistry[None] = ToolRegistry()
    handler = AsyncMock(return_value="ok")
    tools.tool("t", "desc", {"limit": _BOUNDED})(handler)

    result = await tools.call(None, "t", arguments)

    assert result["isError"] is True
    assert result["content"][0]["text"] == f"{INVALID_ARGS}: Argument {fragment}"
    handler.assert_not_awaited()


@pytest.mark.parametrize(
    ("arguments", "seen"),
    [({}, {"limit": 5}), ({"limit": 10}, {"limit": 10}), ({"limit": 1}, {"limit": 1})],
    ids=["default", "at_maximum", "at_minimum"],
)
async def test_call_fills_defaults_and_accepts_the_bounds_themselves(
    arguments: dict[str, Any], seen: dict[str, Any]
) -> None:
    tools: ToolRegistry[None] = ToolRegistry()
    handler = AsyncMock(return_value="ok")
    tools.tool("t", "desc", {"limit": _BOUNDED})(handler)

    await tools.call(None, "t", arguments)

    handler.assert_awaited_once_with(None, seen)
    assert tools.definitions()[0]["inputSchema"]["properties"]["limit"] == _BOUNDED


def test_registration_rejects_unknown_required_names_and_duplicates() -> None:
    tools: ToolRegistry[None] = ToolRegistry()
    with pytest.raises(ValueError, match=r"required names not in properties: \['b'\]"):
        tools.tool("t", "desc", {"a": {"type": "string"}}, ("a", "b"))

    @tools.tool("t", "desc")
    async def _first(_context: None, _args: dict[str, Any]) -> str:
        return "one"

    with pytest.raises(ValueError, match="already registered"):
        tools.tool("t", "desc")

    with pytest.raises(ValueError, match="duplicate required names"):
        tools.tool("t3", "d", {"a": {"type": "string"}}, ("a", "a"))
