"""Coverage for the MCP endpoint: JSON-RPC envelope, method dispatch and every tool."""

from __future__ import annotations

import asyncio
import json
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import jsonschema
import pytest
from aiohttp import web
from pytest_aiohttp.plugin import AiohttpClient

from esphome_device_builder.api.mcp import MCP_PATH, SERVER_NAME, create_mcp_routes
from esphome_device_builder.api.mcp.tools import TOOLS
from esphome_device_builder.constants import __version__
from esphome_device_builder.controllers.auth import AuthError
from esphome_device_builder.controllers.components import ComponentCatalog
from esphome_device_builder.controllers.config import DashboardSettings
from esphome_device_builder.device_builder import DeviceBuilder
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.auth import auth_middleware
from esphome_device_builder.mcp import INTERNAL_ERROR, INVALID_ARGS
from esphome_device_builder.models import (
    AddComponentResponse,
    ComponentCatalogIndexEntry,
    ComponentCategory,
    DevicesResponse,
    DeviceState,
    ErrorCode,
    JobStatus,
    JobType,
    PagedComponentsResponse,
    StreamEvent,
)

from ..conftest import MakeSettingsFactory, StubAuth, make_device, make_job, rpc_post


class _StubDeviceBuilder:
    def __init__(self, settings: DashboardSettings) -> None:
        self.settings = settings
        self.settings.trusted_domains = []
        self.components: ComponentCatalog | None = None
        self.command_handlers: dict[str, Any] = {}
        self.auth = StubAuth()


def _make_app(db: _StubDeviceBuilder, *, with_auth: bool = False) -> web.Application:
    app = web.Application(middlewares=[auth_middleware] if with_auth else [])
    app["device_builder"] = db
    app.router.add_routes(create_mcp_routes())
    return app


_rpc = partial(rpc_post, path=MCP_PATH)


async def _call(client: Any, name: str, arguments: dict[str, Any] | None = None) -> tuple:
    """Return ``(is_error, text)`` for one ``tools/call``."""
    params: dict[str, Any] = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    reply = await _rpc(client, method="tools/call", params=params)
    result = reply["result"]
    return result["isError"], result["content"][0]["text"]


async def _call_json(client: Any, name: str, arguments: dict[str, Any] | None = None) -> Any:
    """Return a successful tool call's text parsed as JSON."""
    is_error, text = await _call(client, name, arguments)
    assert not is_error, text
    return json.loads(text)


@pytest.fixture
def db(make_settings: MakeSettingsFactory) -> _StubDeviceBuilder:
    return _StubDeviceBuilder(make_settings())


@pytest.fixture
async def client(db: _StubDeviceBuilder, aiohttp_client: AiohttpClient) -> Any:
    return await aiohttp_client(_make_app(db))


@pytest.fixture
def catalog_db(
    db: _StubDeviceBuilder, session_component_catalog: ComponentCatalog
) -> _StubDeviceBuilder:
    db.components = session_component_catalog
    db.command_handlers["components/get_component_bodies"] = (
        session_component_catalog.get_component_bodies
    )
    return db


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


async def test_get_is_405_with_allow(client: Any) -> None:
    resp = await client.get(MCP_PATH)
    assert resp.status == 405
    assert resp.headers["Allow"] == "POST"


async def test_delete_is_405(client: Any) -> None:
    assert (await client.delete(MCP_PATH)).status == 405


async def test_route_wins_over_spa_catch_all(
    make_settings: MakeSettingsFactory, aiohttp_client: AiohttpClient
) -> None:
    pytest.importorskip("esphome_device_builder_frontend")
    real_db = DeviceBuilder(make_settings())
    client = await aiohttp_client(real_db.create_app(with_lifecycle=False))

    assert (await client.get(MCP_PATH)).status == 405
    assert (await _rpc(client, method="ping"))["result"] == {}
    assert (await client.get("/some/deep/link")).status == 200


# ---------------------------------------------------------------------------
# Browser gate, initialize, tools/list
# ---------------------------------------------------------------------------


async def test_cross_origin_post_is_rejected_before_the_tool_runs(
    client: Any, db: _StubDeviceBuilder
) -> None:
    handler = AsyncMock(return_value=None)
    db.command_handlers["devices/update_config"] = handler
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "update_config", "arguments": {"configuration": "x", "content": "y"}},
    }
    resp = await client.post(MCP_PATH, json=body, headers={"Origin": "https://evil.example"})
    assert resp.status == 403
    handler.assert_not_awaited()


async def test_same_origin_post_is_allowed(client: Any) -> None:
    origin = f"http://{client.host}:{client.port}"
    reply = await _rpc(client, method="ping", headers={"Origin": origin})
    assert reply["result"] == {}


async def test_trusted_domains_allow_the_origin_and_gate_the_host(
    db: _StubDeviceBuilder, aiohttp_client: AiohttpClient
) -> None:
    db.settings.trusted_domains = ["dashboard.local"]
    client = await aiohttp_client(_make_app(db))
    headers = {"Origin": "https://dashboard.local"}
    # Origin is allowlisted but the request Host (127.0.0.1) is not.
    resp = await client.post(
        MCP_PATH, json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=headers
    )
    assert resp.status == 403
    assert "trusted-domains" in await resp.text()
    db.settings.trusted_domains = ["*"]
    assert (await _rpc(client, method="ping", headers=headers))["result"] == {}


async def test_trusted_site_skips_the_origin_gate(
    db: _StubDeviceBuilder, aiohttp_client: AiohttpClient
) -> None:
    app = _make_app(db)
    app["trusted_site"] = True
    client = await aiohttp_client(app)
    reply = await _rpc(client, method="ping", headers={"Origin": "https://evil.example"})
    assert reply["result"] == {}


async def test_non_json_content_type_is_415(client: Any) -> None:
    resp = await client.post(MCP_PATH, data=b'{"jsonrpc":"2.0","id":1,"method":"ping"}')
    assert resp.status == 415


def test_library_error_words_match_error_code() -> None:
    assert INVALID_ARGS == ErrorCode.INVALID_ARGS
    assert INTERNAL_ERROR == ErrorCode.INTERNAL_ERROR


async def test_initialize_advertises_server_info(client: Any) -> None:
    result = (await _rpc(client, method="initialize", params={}))["result"]
    assert result["serverInfo"] == {"name": SERVER_NAME, "version": __version__}


async def test_tools_list_matches_registry_and_schemas_are_valid(client: Any) -> None:
    listed = (await _rpc(client, method="tools/list"))["result"]["tools"]
    assert [tool["name"] for tool in listed] == list(TOOLS)
    for tool in listed:
        assert tool["description"]
        schema = tool["inputSchema"]
        jsonschema.Draft202012Validator.check_schema(schema)
        assert set(schema["required"]) <= set(schema["properties"])


# ---------------------------------------------------------------------------
# tools/call argument handling and error mapping
# ---------------------------------------------------------------------------


async def test_arguments_may_be_omitted(client: Any, db: _StubDeviceBuilder) -> None:
    db.command_handlers["devices/list"] = AsyncMock(
        return_value=DevicesResponse(configured=[], importable=[])
    )
    reply = await _rpc(client, method="tools/call", params={"name": "list_devices"})
    assert reply["result"] == {"content": [{"type": "text", "text": "[]"}], "isError": False}


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        pytest.param(CommandError(ErrorCode.NOT_FOUND, "gone"), "not_found: gone", id="command"),
        pytest.param(
            AuthError(ErrorCode.NOT_AUTHENTICATED, "no"), "not_authenticated: no", id="auth"
        ),
        pytest.param(
            FileNotFoundError("x"), "internal_error: Tool failed: get_config", id="not_user_facing"
        ),
        pytest.param(RuntimeError("boom"), "internal_error: Tool failed: get_config", id="crash"),
    ],
)
async def test_handler_exceptions_become_tool_errors(
    client: Any, db: _StubDeviceBuilder, exc: Exception, expected: str
) -> None:
    db.command_handlers["devices/get_config"] = AsyncMock(side_effect=exc)
    assert await _call(client, "get_config", {"configuration": "k.yaml"}) == (True, expected)


async def test_missing_command_is_unavailable(client: Any) -> None:
    is_error, text = await _call(client, "get_config", {"configuration": "k.yaml"})
    assert is_error
    assert text == "unavailable: devices/get_config is not available"


# ---------------------------------------------------------------------------
# Device and config tools
# ---------------------------------------------------------------------------


async def test_list_devices_flattens_and_drops_bulk_lists(
    client: Any, db: _StubDeviceBuilder
) -> None:
    online = make_device(
        "kitchen",
        state=DeviceState.ONLINE,
        deployed_version="2026.8.2",
        loaded_integrations=["api", "wifi"],
        has_pending_changes=False,
    )
    db.command_handlers["devices/list"] = AsyncMock(
        return_value=DevicesResponse(configured=[online, make_device("porch")], importable=[])
    )
    rows = await _call_json(client, "list_devices")
    assert [row["configuration"] for row in rows] == ["kitchen.yaml", "porch.yaml"]
    assert rows[0]["state"] == "online"
    assert rows[0]["deployed_version"] == "2026.8.2"
    assert rows[0]["has_pending_changes"] is False
    assert "loaded_integrations" not in rows[0]
    assert "runtime_state" not in rows[0]


async def test_get_config_returns_yaml_verbatim(client: Any, db: _StubDeviceBuilder) -> None:
    handler = AsyncMock(return_value="esphome:\n  name: kitchen\n")
    db.command_handlers["devices/get_config"] = handler
    assert await _call(client, "get_config", {"configuration": "kitchen.yaml"}) == (
        False,
        "esphome:\n  name: kitchen\n",
    )
    assert handler.await_args.kwargs["configuration"] == "kitchen.yaml"
    assert handler.await_args.kwargs["message_id"] == "mcp"


async def test_update_config_forwards_content(client: Any, db: _StubDeviceBuilder) -> None:
    handler = AsyncMock(return_value=None)
    db.command_handlers["devices/update_config"] = handler
    assert await _call(
        client, "update_config", {"configuration": "kitchen.yaml", "content": "esphome: {}"}
    ) == (False, "Saved kitchen.yaml")
    assert handler.await_args.kwargs["content"] == "esphome: {}"


async def test_add_component_returns_the_saved_yaml(client: Any, db: _StubDeviceBuilder) -> None:
    handler = AsyncMock(return_value=AddComponentResponse(yaml="sensor:\n  - platform: dht\n"))
    db.command_handlers["devices/add_component"] = handler
    assert await _call_json(
        client,
        "add_component",
        {"configuration": "kitchen.yaml", "component_id": "sensor.dht", "fields": {"pin": 4}},
    ) == {
        "configuration": "kitchen.yaml",
        "component_id": "sensor.dht",
        "yaml": "sensor:\n  - platform: dht\n",
    }
    assert handler.await_args.kwargs["fields"] == {"pin": 4}


async def test_validate_config_collects_stream_and_strips_ansi(
    client: Any, db: _StubDeviceBuilder
) -> None:
    async def fake_validate(*, client: Any, message_id: str, configuration: str) -> None:
        await client.send_event(message_id, StreamEvent.OUTPUT, "\x1b[32mINFO ok\x1b[0m\n")
        await client.send_event(message_id, StreamEvent.OUTPUT, "second\r")
        await client.send_event(message_id, StreamEvent.OUTPUT, "\\033[31mERROR bad\\033[0m\n")
        await client.send_event(message_id, StreamEvent.RESULT, {"success": True, "code": 0})

    db.command_handlers["devices/validate"] = fake_validate
    assert await _call_json(client, "validate_config", {"configuration": "kitchen.yaml"}) == {
        "success": True,
        "exit_code": 0,
        "output": ["INFO ok", "second", "ERROR bad"],
    }


async def test_validate_config_without_result_frame_is_an_error(
    client: Any, db: _StubDeviceBuilder
) -> None:
    async def swallowed(*, client: Any, message_id: str, configuration: str) -> None:
        await client.send_event(message_id, StreamEvent.OUTPUT, "partial\n")

    db.command_handlers["devices/validate"] = swallowed
    is_error, text = await _call(client, "validate_config", {"configuration": "kitchen.yaml"})
    assert is_error
    assert text == "internal_error: Validation produced no result"


async def test_validate_config_times_out_and_keeps_the_tail(
    client: Any, db: _StubDeviceBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("esphome_device_builder.api.mcp.tools.ESPHOME_CONFIG_TIMEOUT", 0.05)
    cancelled = asyncio.Event()

    async def slow(*, client: Any, message_id: str, configuration: str) -> None:
        await client.send_event(message_id, StreamEvent.OUTPUT, "started\n")
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            # Mirrors stream_subprocess: kill the child, swallow the cancel, return.
            cancelled.set()

    db.command_handlers["devices/validate"] = slow
    assert await _call_json(client, "validate_config", {"configuration": "kitchen.yaml"}) == {
        "success": False,
        "timed_out": True,
        "output": ["started"],
    }
    assert cancelled.is_set()


# ---------------------------------------------------------------------------
# Firmware tools
# ---------------------------------------------------------------------------


async def test_compile_returns_job_id_and_status(client: Any, db: _StubDeviceBuilder) -> None:
    db.command_handlers["firmware/compile"] = AsyncMock(
        return_value=make_job("c1", status=JobStatus.QUEUED)
    )
    assert await _call_json(client, "compile", {"configuration": "kitchen.yaml"}) == {
        "job_id": "c1",
        "status": "queued",
    }


async def test_install_reports_dependent_upload(client: Any, db: _StubDeviceBuilder) -> None:
    compile_job = make_job("c1", status=JobStatus.QUEUED)
    upload = make_job("u1", job_type=JobType.UPLOAD, status=JobStatus.QUEUED, depends_on="c1")
    install = AsyncMock(return_value=compile_job)
    db.command_handlers["firmware/install"] = install
    db.command_handlers["firmware/get_jobs"] = AsyncMock(return_value=[upload, compile_job])
    assert await _call_json(
        client, "install", {"configuration": "kitchen.yaml", "port": "OTA"}
    ) == {"job_id": "c1", "status": "queued", "upload_job_id": "u1", "deferred": False}
    assert install.await_args.kwargs["port"] == "OTA"


async def test_install_deferred_has_no_upload(client: Any, db: _StubDeviceBuilder) -> None:
    deferred = make_job("c1", status=JobStatus.QUEUED, is_deferred_install=True)
    db.command_handlers["firmware/install"] = AsyncMock(return_value=deferred)
    db.command_handlers["firmware/get_jobs"] = AsyncMock(return_value=[deferred])
    assert await _call_json(client, "install", {"configuration": "kitchen.yaml"}) == {
        "job_id": "c1",
        "status": "queued",
        "upload_job_id": None,
        "deferred": True,
    }


async def test_install_without_upload_and_not_deferred_is_an_error(
    client: Any, db: _StubDeviceBuilder
) -> None:
    compile_job = make_job("c1", status=JobStatus.QUEUED)
    db.command_handlers["firmware/install"] = AsyncMock(return_value=compile_job)
    db.command_handlers["firmware/get_jobs"] = AsyncMock(return_value=[compile_job])
    is_error, text = await _call(client, "install", {"configuration": "kitchen.yaml"})
    assert is_error
    assert text == "internal_error: Install chain for c1 has no upload job"


async def test_get_job_unknown_is_not_found(client: Any, db: _StubDeviceBuilder) -> None:
    db.command_handlers["firmware/get_job"] = AsyncMock(return_value=None)
    assert await _call(client, "get_job", {"job_id": "nope"}) == (
        True,
        "not_found: Job not found: nope",
    )


async def test_get_job_tails_ram_output_for_running_job(
    client: Any, db: _StubDeviceBuilder
) -> None:
    job = make_job(output=[f"line {i}\n" for i in range(5)], progress=42)
    db.command_handlers["firmware/get_job"] = AsyncMock(return_value=job)
    data = await _call_json(client, "get_job", {"job_id": "job1", "tail_lines": 2})
    assert data["output"] == ["line 3", "line 4"]
    assert data["status"] == "running"
    assert data["progress"] == 42
    assert data["queued_update_armed"] is False


async def test_get_job_reads_sidecar_for_terminal_job(client: Any, db: _StubDeviceBuilder) -> None:
    job = make_job(status=JobStatus.COMPLETED, exit_code=0)
    db.command_handlers["firmware/get_job"] = AsyncMock(return_value=job)
    with patch(
        "esphome_device_builder.controllers.firmware.follow.read_job_output",
        return_value=["a\n", "b\n", "c\n"],
    ) as read:
        data = await _call_json(client, "get_job", {"job_id": "job1"})
    read.assert_called_once_with("job1")
    assert data["output"] == ["a", "b", "c"]
    assert data["exit_code"] == 0
    assert data["status"] == "completed"


async def test_get_job_zero_tail_skips_the_output_read(client: Any, db: _StubDeviceBuilder) -> None:
    job = make_job(status=JobStatus.COMPLETED, exit_code=0)
    db.command_handlers["firmware/get_job"] = AsyncMock(return_value=job)
    with patch("esphome_device_builder.controllers.firmware.follow.read_job_output") as read:
        data = await _call_json(client, "get_job", {"job_id": "job1", "tail_lines": 0})
    read.assert_not_called()
    assert data["output"] == []


async def test_cancel_job(client: Any, db: _StubDeviceBuilder) -> None:
    handler = AsyncMock(return_value=None)
    db.command_handlers["firmware/cancel"] = handler
    assert await _call(client, "cancel_job", {"job_id": "job1"}) == (False, "Cancelled job1")
    assert handler.await_args.kwargs["job_id"] == "job1"


# ---------------------------------------------------------------------------
# Catalog tools
# ---------------------------------------------------------------------------


async def test_search_components_projects_index_rows(client: Any, db: _StubDeviceBuilder) -> None:
    entry = ComponentCatalogIndexEntry(
        id="sensor.dht",
        name="DHT",
        description="Temperature",
        category=ComponentCategory.SENSOR,
        docs_url="https://esphome.io/components/sensor/dht",
    )
    handler = AsyncMock(return_value=PagedComponentsResponse(components=[entry]))
    db.command_handlers["components/get_components"] = handler
    assert await _call_json(client, "search_components", {"query": "dht"}) == [
        {
            "id": "sensor.dht",
            "name": "DHT",
            "description": "Temperature",
            "category": "sensor",
            "docs_url": "https://esphome.io/components/sensor/dht",
        }
    ]
    assert handler.await_args.kwargs["limit"] == 20

    await _call(client, "search_components", {"query": "dht", "limit": 3})
    assert handler.await_args.kwargs["limit"] == 3


async def test_get_component_projects_the_catalog_body(
    client: Any, catalog_db: _StubDeviceBuilder
) -> None:
    body = await _call_json(client, "get_component", {"component_id": "sensor.dht"})
    assert body["id"] == "sensor.dht"
    assert body["docs_url"].startswith("https://esphome.io/")
    entries = {entry["key"]: entry for entry in body["config_entries"]}
    assert {"pin", "model", "update_interval"} <= set(entries)
    assert "id" not in entries  # advanced
    assert {option["value"] for option in entries["model"]["options"]} >= {"DHT11", "DHT22"}
    assert not any("hidden" in entry or entry.get("advanced") for entry in entries.values())
    assert "null" not in json.dumps(body)


async def test_get_component_include_advanced(client: Any, catalog_db: _StubDeviceBuilder) -> None:
    slim = await _call_json(client, "get_component", {"component_id": "sensor.dht"})
    full = await _call_json(
        client, "get_component", {"component_id": "sensor.dht", "include_advanced": True}
    )
    assert "id" in {entry["key"] for entry in full["config_entries"]}
    assert len(json.dumps(full)) > len(json.dumps(slim))


async def test_get_component_unknown_is_not_found(
    client: Any, catalog_db: _StubDeviceBuilder
) -> None:
    assert await _call(client, "get_component", {"component_id": "sensor.nope"}) == (
        True,
        "not_found: Unknown component: sensor.nope",
    )


async def test_get_config_components_lists_catalog_rows(
    client: Any,
    catalog_db: _StubDeviceBuilder,
    make_settings: MakeSettingsFactory,
    tmp_path: Path,
) -> None:
    make_settings(with_core_path=True)
    (tmp_path / "kitchen.yaml").write_text(
        "substitutions:\n  room: kitchen\n"
        "esphome:\n  name: ${room}\n"
        "sensor:\n  - platform: dht\n    pin: 4\n"
        "notacomponent:\n  x: 1\n"
    )
    rows = {
        row["id"]: row
        for row in await _call_json(
            client, "get_config_components", {"configuration": "kitchen.yaml"}
        )
    }
    assert list(rows) == ["esphome", "sensor", "sensor.dht", "notacomponent"]
    assert rows["sensor.dht"]["name"]
    assert rows["sensor.dht"]["docs_url"].startswith("https://esphome.io/")
    assert rows["notacomponent"] == {"id": "notacomponent"}


@pytest.mark.parametrize(
    ("configuration", "content", "prefix"),
    [
        pytest.param("nope.yaml", None, "not_found: Device 'nope.yaml' not found", id="missing"),
        pytest.param(
            "bad.yaml",
            "- just\n- a list\n",
            "invalid_args: bad.yaml: bad.yaml is not a mapping",
            id="non_mapping",
        ),
        pytest.param("syntax.yaml", ": :", "invalid_args: syntax.yaml: ", id="syntax"),
        pytest.param("../../etc/passwd", None, "invalid_args: ", id="traversal"),
    ],
)
async def test_get_config_components_failures(
    client: Any,
    catalog_db: _StubDeviceBuilder,
    make_settings: MakeSettingsFactory,
    tmp_path: Path,
    configuration: str,
    content: str | None,
    prefix: str,
) -> None:
    make_settings(with_core_path=True)
    if content is not None:
        (tmp_path / configuration).write_text(content)
    is_error, text = await _call(client, "get_config_components", {"configuration": configuration})
    assert is_error
    assert text.startswith(prefix)


async def test_get_config_components_times_out(
    client: Any,
    catalog_db: _StubDeviceBuilder,
    make_settings: MakeSettingsFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_settings(with_core_path=True)
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n")
    monkeypatch.setattr("esphome_device_builder.api.mcp.tools.ESPHOME_CONFIG_TIMEOUT", 0.05)

    async def stalled(*_args: Any) -> dict[str, Any]:
        await asyncio.sleep(10)
        return {}

    monkeypatch.setattr("esphome_device_builder.api.mcp.tools.run_in_executor", stalled)
    is_error, text = await _call(client, "get_config_components", {"configuration": "kitchen.yaml"})
    assert is_error
    assert text.startswith("unavailable: Resolving kitchen.yaml exceeded")


@pytest.mark.parametrize("tool", ["get_config", "update_config", "get_config_components"])
async def test_secrets_file_is_refused(
    client: Any, catalog_db: _StubDeviceBuilder, tool: str
) -> None:
    handler = AsyncMock(return_value="wifi_password: hunter2\n")
    catalog_db.command_handlers["devices/get_config"] = handler
    catalog_db.command_handlers["devices/update_config"] = handler
    args = {"configuration": "secrets.yaml"}
    if tool == "update_config":
        args["content"] = "x: 1"
    is_error, text = await _call(client, tool, args)
    assert is_error
    assert text == "invalid_args: secrets.yaml is not available over MCP"
    handler.assert_not_awaited()


async def test_tail_lines_and_limit_are_clamped(client: Any, db: _StubDeviceBuilder) -> None:
    db.command_handlers["firmware/get_job"] = AsyncMock(
        return_value=make_job(output=[f"{i}\n" for i in range(1500)])
    )
    data = await _call_json(client, "get_job", {"job_id": "job1", "tail_lines": 5000})
    assert len(data["output"]) == 1000
    search = AsyncMock(return_value=PagedComponentsResponse(components=[]))
    db.command_handlers["components/get_components"] = search
    await _call(client, "search_components", {"query": "x", "limit": 5000})
    assert search.await_args.kwargs["limit"] == 100


async def test_get_config_components_without_catalog_is_unavailable(client: Any) -> None:
    is_error, text = await _call(client, "get_config_components", {"configuration": "k.yaml"})
    assert is_error
    assert text.startswith("unavailable: ")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("using_password", "status"), [(True, 401), (False, 200)])
async def test_password_gate(
    db: _StubDeviceBuilder, aiohttp_client: AiohttpClient, using_password: bool, status: int
) -> None:
    db.settings.using_password = using_password
    client = await aiohttp_client(_make_app(db, with_auth=True))
    resp = await client.post(MCP_PATH, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert resp.status == status
