"""Coverage for the MCP endpoint route and the Device Builder tools."""

from __future__ import annotations

import asyncio
import json
from functools import partial
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jsonschema
import pytest
from aiohttp import web
from pytest_aiohttp.plugin import AiohttpClient

from esphome_device_builder.api.mcp import MCP_PATH, SERVER_NAME, create_mcp_routes
from esphome_device_builder.api.mcp.tools import TOOLS, _refuse_secrets
from esphome_device_builder.constants import __version__
from esphome_device_builder.controllers.auth import AuthError
from esphome_device_builder.controllers.components import ComponentCatalog
from esphome_device_builder.controllers.config import DashboardSettings
from esphome_device_builder.controllers.devices import DevicesController
from esphome_device_builder.device_builder import DeviceBuilder
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.auth import auth_middleware
from esphome_device_builder.mcp import INTERNAL_ERROR, INVALID_ARGS
from esphome_device_builder.models import (
    AddComponentResponse,
    BoardCatalogIndex,
    ComponentCatalogIndexEntry,
    ComponentCategory,
    DevicesResponse,
    DeviceState,
    ErrorCode,
    JobStatus,
    JobType,
    PagedBoardsResponse,
    PagedComponentsResponse,
    StreamEvent,
    WizardResponse,
)

from ..conftest import MakeSettingsFactory, StubAuth, make_device, make_job, rpc_post

_rpc = partial(rpc_post, path=MCP_PATH)
_INIT = {
    "protocolVersion": "2025-11-25",
    "capabilities": {},
    "clientInfo": {"name": "test", "version": "0"},
}
_CONFIG_TOOLS = (
    ("get_config", {}),
    ("update_config", {"content": "x: 1"}),
    ("add_component", {"component_id": "wifi"}),
    ("validate_config", {}),
    ("compile", {}),
    ("install", {}),
    ("get_config_components", {}),
)
_CONFIG_COMMANDS = (
    "devices/get_config",
    "devices/update_config",
    "devices/add_component",
    "devices/validate",
    "firmware/compile",
    "firmware/install",
)


class _StubDeviceBuilder:
    def __init__(self, settings: DashboardSettings) -> None:
        self.settings = settings
        self.settings.trusted_domains = []
        self.components: ComponentCatalog | None = None
        self.command_handlers: dict[str, Any] = {}
        self.auth = StubAuth()
        self.devices = MagicMock(spec=DevicesController)
        self.devices.get_by_configuration.return_value = None


def _make_app(db: _StubDeviceBuilder, *, with_auth: bool = False) -> web.Application:
    app = web.Application(middlewares=[auth_middleware] if with_auth else [])
    app["device_builder"] = db
    app.router.add_routes(create_mcp_routes())
    return app


async def _call(client: Any, name: str, arguments: dict[str, Any] | None = None) -> tuple:
    """Return ``(is_error, text)`` for one ``tools/call``."""
    params: dict[str, Any] = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    result = (await _rpc(client, method="tools/call", params=params))["result"]
    return result["isError"], result["content"][0]["text"]


async def _call_json(client: Any, name: str, arguments: dict[str, Any] | None = None) -> Any:
    """Return a successful tool call's text parsed as JSON."""
    is_error, text = await _call(client, name, arguments)
    assert not is_error, text
    return json.loads(text)


def _validate_stub(
    frames: list[tuple[str, Any]], *, sleep: float = 0, swallow_cancel: bool = False
) -> Any:
    """Build a ``devices/validate`` stand-in that emits *frames*, then optionally stalls."""

    async def validate(*, client: Any, message_id: str, configuration: str) -> None:
        for event, data in frames:
            await client.send_event(message_id, event, data)
        if sleep:
            try:
                await asyncio.sleep(sleep)
            except asyncio.CancelledError:
                if not swallow_cancel:
                    raise

    return validate


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
# Route
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["get", "delete"])
async def test_non_post_is_405(client: Any, method: str) -> None:
    assert (await getattr(client, method)(MCP_PATH)).status == 405


async def test_route_wins_over_spa_catch_all(
    make_settings: MakeSettingsFactory, aiohttp_client: AiohttpClient
) -> None:
    pytest.importorskip("esphome_device_builder_frontend")
    real_db = DeviceBuilder(make_settings())
    client = await aiohttp_client(real_db.create_app(with_lifecycle=False))
    assert (await _rpc(client, method="ping"))["result"] == {}
    assert (await client.get("/some/deep/link")).status == 200


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
    assert (await _rpc(client, method="ping", headers={"Origin": origin}))["result"] == {}


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


@pytest.mark.parametrize(("using_password", "status"), [(True, 401), (False, 200)])
async def test_password_gate(
    db: _StubDeviceBuilder, aiohttp_client: AiohttpClient, using_password: bool, status: int
) -> None:
    db.settings.using_password = using_password
    client = await aiohttp_client(_make_app(db, with_auth=True))
    resp = await client.post(MCP_PATH, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert resp.status == status


# ---------------------------------------------------------------------------
# Server identity and tool registry
# ---------------------------------------------------------------------------


async def test_initialize_advertises_server_info(client: Any) -> None:
    result = (await _rpc(client, method="initialize", params=_INIT))["result"]
    assert result["serverInfo"] == {"name": SERVER_NAME, "version": __version__}


async def test_tools_list_matches_registry_and_schemas_are_valid(client: Any) -> None:
    listed = (await _rpc(client, method="tools/list"))["result"]["tools"]
    assert [tool["name"] for tool in listed] == list(TOOLS)
    for tool in listed:
        assert tool["description"]
        jsonschema.Draft202012Validator.check_schema(tool["inputSchema"])


def test_library_error_words_match_error_code() -> None:
    assert INVALID_ARGS == ErrorCode.INVALID_ARGS
    assert INTERNAL_ERROR == ErrorCode.INTERNAL_ERROR


@pytest.mark.parametrize(
    ("arguments", "fragment"),
    [
        pytest.param({}, "Missing required argument: job_id", id="missing"),
        pytest.param({"job_id": "j", "extra": 1}, "Unknown argument: extra", id="unknown"),
        pytest.param({"job_id": 5}, "job_id must be string", id="wrong_type"),
    ],
)
async def test_argument_validation_is_a_tool_error(
    client: Any, arguments: dict[str, Any], fragment: str
) -> None:
    is_error, text = await _call(client, "get_job", arguments)
    assert is_error
    assert text.startswith(f"{ErrorCode.INVALID_ARGS.value}: ")
    assert fragment in text


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
# Secrets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["secrets.yaml", "SECRETS.YAML", "./Secrets.yaml", "secrets.yml"])
def test_refuse_secrets_matches_every_spelling(name: str) -> None:
    with pytest.raises(CommandError) as excinfo:
        _refuse_secrets(name)
    assert excinfo.value.code is ErrorCode.INVALID_ARGS
    _refuse_secrets("kitchen.yaml")
    _refuse_secrets(None)


@pytest.mark.parametrize(("tool", "extra"), _CONFIG_TOOLS)
async def test_secrets_file_is_refused_by_every_config_tool(
    client: Any, catalog_db: _StubDeviceBuilder, tool: str, extra: dict[str, Any]
) -> None:
    handler = AsyncMock(return_value="wifi_password: hunter2\n")
    for command in _CONFIG_COMMANDS:
        catalog_db.command_handlers[command] = handler
    is_error, text = await _call(client, tool, {"configuration": "secrets.yaml"} | extra)
    assert is_error
    assert text == "invalid_args: secrets.yaml is not available over MCP"
    handler.assert_not_awaited()


# ---------------------------------------------------------------------------
# Device and config tools
# ---------------------------------------------------------------------------


async def test_list_devices_keeps_scalar_fields_only(client: Any, db: _StubDeviceBuilder) -> None:
    online = make_device(
        "kitchen",
        state=DeviceState.ONLINE,
        loaded_integrations=["api", "wifi"],
        has_pending_changes=False,
    )
    db.command_handlers["devices/list"] = AsyncMock(
        return_value=DevicesResponse(configured=[online, make_device("porch")], importable=[])
    )
    rows = await _call_json(client, "list_devices")
    assert [row["configuration"] for row in rows] == ["kitchen.yaml", "porch.yaml"]
    assert rows[0]["state"] == "online"
    assert rows[0]["has_pending_changes"] is False
    assert not any(isinstance(value, list) for row in rows for value in row.values())


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
    db.command_handlers["devices/validate"] = _validate_stub(
        [
            (StreamEvent.OUTPUT, "\x1b[32mINFO ok\x1b[0m\n"),
            (StreamEvent.OUTPUT, "second\r"),
            (StreamEvent.OUTPUT, "\\033[31mERROR bad\\033[0m\n"),
            (StreamEvent.RESULT, {"success": True, "code": 0}),
        ]
    )
    assert await _call_json(client, "validate_config", {"configuration": "kitchen.yaml"}) == {
        "success": True,
        "exit_code": 0,
        "output": ["INFO ok", "second", "ERROR bad"],
        "truncated": False,
    }


async def test_validate_config_without_result_frame_is_an_error(
    client: Any, db: _StubDeviceBuilder
) -> None:
    db.command_handlers["devices/validate"] = _validate_stub([(StreamEvent.OUTPUT, "partial\n")])
    is_error, text = await _call(client, "validate_config", {"configuration": "kitchen.yaml"})
    assert is_error
    assert text == "internal_error: Validation produced no result"


@pytest.mark.parametrize("swallow_cancel", [True, False], ids=["swallowed", "propagated"])
async def test_validate_config_times_out_and_keeps_the_tail(
    client: Any, db: _StubDeviceBuilder, monkeypatch: pytest.MonkeyPatch, swallow_cancel: bool
) -> None:
    monkeypatch.setattr("esphome_device_builder.api.mcp.tools.ESPHOME_CONFIG_TIMEOUT", 0.05)
    db.command_handlers["devices/validate"] = _validate_stub(
        [(StreamEvent.OUTPUT, "started\n")], sleep=10, swallow_cancel=swallow_cancel
    )
    assert await _call_json(client, "validate_config", {"configuration": "kitchen.yaml"}) == {
        "success": False,
        "timed_out": True,
        "output": ["started"],
        "truncated": False,
    }


async def test_validate_config_reports_truncation(client: Any, db: _StubDeviceBuilder) -> None:
    frames: list[tuple[str, Any]] = [(StreamEvent.OUTPUT, f"{i}\n") for i in range(60)]
    frames.append((StreamEvent.RESULT, {"success": True, "code": 0}))
    db.command_handlers["devices/validate"] = _validate_stub(frames)
    data = await _call_json(client, "validate_config", {"configuration": "kitchen.yaml"})
    assert data["truncated"] is True
    assert data["output"][0] == "10"
    assert len(data["output"]) == 50


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


async def test_get_config_components_lists_catalog_rows_from_the_scan(
    client: Any, catalog_db: _StubDeviceBuilder
) -> None:
    catalog_db.devices.get_by_configuration.return_value = make_device(
        "kitchen",
        component_ids=["substitutions", "esphome", "sensor", "sensor.dht", "sensor.hunter2"],
    )
    rows = await _call_json(client, "get_config_components", {"configuration": "kitchen.yaml"})
    catalog_db.devices.get_by_configuration.assert_called_with("kitchen.yaml")
    # Domain keys such as ``sensor`` have no catalog entry; a stray value is never echoed.
    assert [row["id"] for row in rows] == ["substitutions", "esphome", "sensor.dht"]
    assert rows[2]["name"]
    assert rows[2]["docs_url"].startswith("https://esphome.io/")
    assert "hunter2" not in json.dumps(rows)


@pytest.mark.parametrize("configuration", ["nope.yaml", "../../etc/passwd"])
async def test_get_config_components_unknown_device_is_not_found(
    client: Any, catalog_db: _StubDeviceBuilder, configuration: str
) -> None:
    is_error, text = await _call(client, "get_config_components", {"configuration": configuration})
    assert is_error
    assert text.startswith("not_found: ")


async def test_get_config_components_without_catalog_is_unavailable(client: Any) -> None:
    is_error, text = await _call(client, "get_config_components", {"configuration": "k.yaml"})
    assert is_error
    assert text.startswith("unavailable: ")


# ---------------------------------------------------------------------------
# Device creation and secrets
# ---------------------------------------------------------------------------


async def test_search_boards_projects_index_rows(client: Any, db: _StubDeviceBuilder) -> None:
    board = BoardCatalogIndex(
        id="esp32dev", name="ESP32 Dev Module", description="", manufacturer="Espressif"
    )
    handler = AsyncMock(return_value=PagedBoardsResponse(boards=[board]))
    db.command_handlers["boards/get_boards"] = handler
    rows = await _call_json(client, "search_boards", {"query": "esp32", "limit": 5000})
    assert rows[0]["id"] == "esp32dev"
    assert rows[0]["name"] == "ESP32 Dev Module"
    assert handler.await_args.kwargs["limit"] == 100


async def test_list_secret_names_returns_names_only(client: Any, db: _StubDeviceBuilder) -> None:
    db.command_handlers["config/get_secrets"] = AsyncMock(
        return_value=["wifi_password", "wifi_ssid"]
    )
    assert await _call_json(client, "list_secret_names") == ["wifi_password", "wifi_ssid"]


async def test_set_secret_is_write_only(client: Any, db: _StubDeviceBuilder) -> None:
    handler = AsyncMock(return_value={"created": True})
    db.command_handlers["config/set_secret"] = handler
    assert await _call_json(
        client, "set_secret", {"name": "wifi_password", "value": "hunter2"}
    ) == {
        "name": "wifi_password",
        "created": True,
    }
    assert handler.await_args.kwargs == {
        "client": handler.await_args.kwargs["client"],
        "message_id": "mcp",
        "key": "wifi_password",
        "value": "hunter2",
        "overwrite": True,
    }


async def test_create_device_never_passes_credentials(client: Any, db: _StubDeviceBuilder) -> None:
    handler = AsyncMock(return_value=WizardResponse(configuration="porch.yaml"))
    db.command_handlers["devices/create"] = handler
    assert await _call_json(
        client, "create_device", {"name": "porch", "friendly_name": "Porch", "board_id": "esp32dev"}
    ) == {"configuration": "porch.yaml", "warning": None}
    assert "ssid" not in handler.await_args.kwargs
    assert "psk" not in handler.await_args.kwargs
    is_error, text = await _call(client, "create_device", {"name": "x", "ssid": "net"})
    assert is_error
    assert "Unknown argument: ssid" in text
