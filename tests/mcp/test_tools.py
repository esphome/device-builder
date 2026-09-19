"""Tool registry: registration, argument validation, error translation and result shaping."""

from __future__ import annotations

from typing import Any

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
    "prop", [{}, {"type": "enum"}, {"oneOf": []}], ids=["none", "enum", "oneOf"]
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


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        pytest.param("text", (False, "plain"), id="string_result"),
        pytest.param("data", (False, '{"a":1}'), id="json_result"),
        pytest.param("fail", (True, "not_found: gone"), id="tool_error"),
        pytest.param("busy", (True, "busy: try later"), id="translated"),
        pytest.param("crash", (True, f"{INTERNAL_ERROR}: Tool failed: crash"), id="untranslated"),
        pytest.param("weird", (True, f"{INTERNAL_ERROR}: Tool failed: weird"), id="unserialisable"),
    ],
)
async def test_call_shapes_results_and_errors(
    tools: ToolRegistry[None], name: str, expected: tuple[bool, str]
) -> None:
    result = await tools.call(None, name, {})
    assert (result["isError"], result["content"][0]["text"]) == expected
    assert result["content"][0]["type"] == "text"
