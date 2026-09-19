"""Tool registry: argument validation, error translation and result shaping."""

from __future__ import annotations

from typing import Any

import jsonschema
import pytest

from esphome_device_builder.mcp import (
    INTERNAL_ERROR,
    INVALID_ARGS,
    McpToolError,
    ToolRegistry,
    validate_args,
)

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
    assert definition["name"] == "t"
    assert list(tools) == ["t"]


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
        pytest.param({"name": "x", "count": 1.5}, "count must be integer", id="float_as_int"),
    ],
)
def test_validate_args_rejects(arguments: dict[str, Any], fragment: str) -> None:
    with pytest.raises(McpToolError) as excinfo:
        validate_args(_SCHEMA, arguments)
    assert excinfo.value.code == INVALID_ARGS
    assert fragment in excinfo.value.message


async def test_call_shapes_string_and_json_results() -> None:
    tools: ToolRegistry[None] = ToolRegistry()

    @tools.tool("text", "")
    async def _text(_context: None, _args: dict[str, Any]) -> str:
        return "plain"

    @tools.tool("data", "")
    async def _data(_context: None, _args: dict[str, Any]) -> dict[str, Any]:
        return {"a": 1}

    assert await tools.call(None, "text", {}) == {
        "content": [{"type": "text", "text": "plain"}],
        "isError": False,
    }
    assert await tools.call(None, "data", {}) == {
        "content": [{"type": "text", "text": '{"a":1}'}],
        "isError": False,
    }


async def test_call_reports_tool_errors_with_code_prefix() -> None:
    tools: ToolRegistry[None] = ToolRegistry()

    @tools.tool("fail", "")
    async def _fail(_context: None, _args: dict[str, Any]) -> None:
        raise McpToolError("not_found", "gone")

    assert await tools.call(None, "fail", {}) == {
        "content": [{"type": "text", "text": "not_found: gone"}],
        "isError": True,
    }


async def test_call_translates_foreign_exceptions() -> None:
    def translate(err: Exception) -> McpToolError | None:
        return McpToolError("busy", str(err)) if isinstance(err, BlockingIOError) else None

    tools: ToolRegistry[None] = ToolRegistry(translate=translate)

    @tools.tool("busy", "")
    async def _busy(_context: None, _args: dict[str, Any]) -> None:
        raise BlockingIOError("try later")

    @tools.tool("crash", "")
    async def _crash(_context: None, _args: dict[str, Any]) -> None:
        raise RuntimeError("boom")

    assert (await tools.call(None, "busy", {}))["content"][0]["text"] == "busy: try later"
    crash = await tools.call(None, "crash", {})
    assert crash["isError"] is True
    assert crash["content"][0]["text"] == f"{INTERNAL_ERROR}: Tool failed: crash"


async def test_call_reports_unserialisable_results_in_the_envelope() -> None:
    tools: ToolRegistry[None] = ToolRegistry()

    @tools.tool("weird", "")
    async def _weird(_context: None, _args: dict[str, Any]) -> object:
        return object()

    result = await tools.call(None, "weird", {})
    assert result["isError"] is True
    assert result["content"][0]["text"].startswith(f"{INTERNAL_ERROR}: ")
