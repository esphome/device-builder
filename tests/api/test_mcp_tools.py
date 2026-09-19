"""Coverage for the Device Builder MCP tools."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from esphome_device_builder.api.mcp.tools import TOOLS, _refuse_secrets
from esphome_device_builder.controllers.automations import AutomationsController
from esphome_device_builder.controllers.boards import BoardCatalog
from esphome_device_builder.helpers.api import CommandError
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
    WizardResponse,
)

from ..conftest import (
    McpStubDeviceBuilder,
    make_device,
    make_job,
)
from .conftest import (
    mcp_call,
    mcp_call_json,
    validate_stub,
)

_CONFIG_TOOLS = (
    ("get_config", {}),
    ("update_config", {"content": "x: 1"}),
    ("add_component", {"component_id": "wifi"}),
    ("validate_config", {}),
    ("compile", {}),
    ("install", {}),
    ("get_config_components", {}),
    ("list_automations", {}),
    ("get_available_automations", {}),
    ("delete_automation", {"location": {"kind": "script", "index": 0}}),
)
_CONFIG_COMMANDS = (
    "devices/get_config",
    "devices/update_config",
    "devices/add_component",
    "devices/validate",
    "firmware/compile",
    "firmware/install",
    "automations/parse",
    "automations/get_available",
    "automations/delete",
)


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "secrets.yaml",
        "SECRETS.YAML",
        "./Secrets.yaml",
        "secrets.yml",
        "secrets.yaml.",
        "secrets.yaml ",
        "secrets.yaml::$DATA",
        "SECRET~1.YAM",
        "notes.txt",
    ],
)
def test_refuse_secrets_matches_every_spelling(name: str) -> None:
    with pytest.raises(CommandError) as excinfo:
        _refuse_secrets(name)
    assert excinfo.value.code is ErrorCode.INVALID_ARGS
    _refuse_secrets("kitchen.yaml")
    _refuse_secrets(None)


def test_every_configuration_tool_is_covered() -> None:
    takes_config = {
        name for name, tool in TOOLS.items() if "configuration" in tool.schema["properties"]
    }
    assert takes_config == {name for name, _ in _CONFIG_TOOLS}


@pytest.mark.parametrize(("tool", "extra"), _CONFIG_TOOLS)
async def test_secrets_file_is_refused_by_every_config_tool(
    mcp_client: Any, mcp_catalog_db: McpStubDeviceBuilder, tool: str, extra: dict[str, Any]
) -> None:
    handler = AsyncMock(return_value="wifi_password: hunter2\n")
    for command in _CONFIG_COMMANDS:
        mcp_catalog_db.command_handlers[command] = handler
    is_error, text = await mcp_call(mcp_client, tool, {"configuration": "secrets.yaml"} | extra)
    assert is_error
    assert text == "invalid_args: secrets.yaml is not available over MCP"
    handler.assert_not_awaited()


# ---------------------------------------------------------------------------
# Device and config tools
# ---------------------------------------------------------------------------


async def test_list_devices_keeps_scalar_fields_only(
    mcp_client: Any, mcp_db: McpStubDeviceBuilder
) -> None:
    online = make_device(
        "kitchen",
        state=DeviceState.ONLINE,
        loaded_integrations=["api", "wifi"],
        has_pending_changes=False,
    )
    mcp_db.command_handlers["devices/list"] = AsyncMock(
        return_value=DevicesResponse(configured=[online, make_device("porch")], importable=[])
    )
    rows = await mcp_call_json(mcp_client, "list_devices")
    assert [row["configuration"] for row in rows] == ["kitchen.yaml", "porch.yaml"]
    assert rows[0]["state"] == "online"
    assert rows[0]["has_pending_changes"] is False
    assert not any(isinstance(value, list) for row in rows for value in row.values())


async def test_get_config_returns_yaml_verbatim(
    mcp_client: Any, mcp_db: McpStubDeviceBuilder
) -> None:
    handler = AsyncMock(return_value="esphome:\n  name: kitchen\n")
    mcp_db.command_handlers["devices/get_config"] = handler
    assert await mcp_call(mcp_client, "get_config", {"configuration": "kitchen.yaml"}) == (
        False,
        "esphome:\n  name: kitchen\n",
    )
    assert handler.await_args.kwargs["configuration"] == "kitchen.yaml"
    assert handler.await_args.kwargs["message_id"] == "mcp"


async def test_update_config_forwards_content(
    mcp_client: Any, mcp_db: McpStubDeviceBuilder
) -> None:
    handler = AsyncMock(return_value=None)
    mcp_db.command_handlers["devices/update_config"] = handler
    assert await mcp_call(
        mcp_client, "update_config", {"configuration": "kitchen.yaml", "content": "esphome: {}"}
    ) == (False, "Saved kitchen.yaml")
    assert handler.await_args.kwargs["content"] == "esphome: {}"


async def test_add_component_returns_the_saved_yaml(
    mcp_client: Any, mcp_db: McpStubDeviceBuilder
) -> None:
    handler = AsyncMock(return_value=AddComponentResponse(yaml="sensor:\n  - platform: dht\n"))
    mcp_db.command_handlers["devices/add_component"] = handler
    assert await mcp_call_json(
        mcp_client,
        "add_component",
        {"configuration": "kitchen.yaml", "component_id": "sensor.dht", "fields": {"pin": 4}},
    ) == {
        "configuration": "kitchen.yaml",
        "component_id": "sensor.dht",
        "yaml": "sensor:\n  - platform: dht\n",
    }
    assert handler.await_args.kwargs["fields"] == {"pin": 4}


async def test_validate_config_collects_stream_and_strips_ansi(
    mcp_client: Any, mcp_db: McpStubDeviceBuilder
) -> None:
    mcp_db.command_handlers["devices/validate"] = validate_stub(
        [
            (StreamEvent.OUTPUT, "\x1b[32mINFO ok\x1b[0m\n"),
            (StreamEvent.OUTPUT, "second\r"),
            (StreamEvent.OUTPUT, "\\033[31mERROR bad\\033[0m\n"),
            (StreamEvent.RESULT, {"success": True, "code": 0}),
        ]
    )
    assert await mcp_call_json(
        mcp_client, "validate_config", {"configuration": "kitchen.yaml"}
    ) == {
        "success": True,
        "exit_code": 0,
        "output": ["INFO ok", "second", "ERROR bad"],
        "truncated": False,
    }


async def test_validate_config_removes_secret_values(
    mcp_client: Any, mcp_db: McpStubDeviceBuilder
) -> None:
    (mcp_db.settings.config_dir / "secrets.yaml").write_text(
        "mqtt_user: alice_smith\nmqtt_port: 1883\nflag: true\n"
    )
    (mcp_db.settings.config_dir / "secrets.yml").write_text(
        "ota_pass: 123456\nnested:\n  - token: abcdefgh\n"
    )
    mcp_db.command_handlers["devices/validate"] = validate_stub(
        [
            (StreamEvent.OUTPUT, "  username: alice_smith\n"),
            (StreamEvent.OUTPUT, "  port: 1883 password: 123456\n"),
            (StreamEvent.OUTPUT, "  token: abcdefgh keep: true\n"),
            (StreamEvent.RESULT, {"success": True, "code": 0}),
        ]
    )
    data = await mcp_call_json(mcp_client, "validate_config", {"configuration": "kitchen.yaml"})
    assert data["output"] == [
        "  username: <removed>",
        "  port: 1883 password: <removed>",
        "  token: <removed> keep: true",
    ]


@pytest.mark.parametrize("content", ["", "# nothing yet\n"], ids=["empty", "comment_only"])
async def test_validate_config_accepts_an_empty_secrets_file(
    mcp_client: Any, mcp_db: McpStubDeviceBuilder, content: str
) -> None:
    (mcp_db.settings.config_dir / "secrets.yaml").write_text(content)
    mcp_db.command_handlers["devices/validate"] = validate_stub(
        [(StreamEvent.OUTPUT, "ok\n"), (StreamEvent.RESULT, {"success": True, "code": 0})]
    )
    data = await mcp_call_json(mcp_client, "validate_config", {"configuration": "kitchen.yaml"})
    assert data["output"] == ["ok"]


async def test_validate_config_withholds_output_when_secrets_are_unreadable(
    mcp_client: Any, mcp_db: McpStubDeviceBuilder
) -> None:
    (mcp_db.settings.config_dir / "secrets.yaml").write_text("- not\n- a mapping\n")
    mcp_db.command_handlers["devices/validate"] = validate_stub(
        [(StreamEvent.OUTPUT, "x\n"), (StreamEvent.RESULT, {"success": True, "code": 0})]
    )
    is_error, text = await mcp_call(
        mcp_client, "validate_config", {"configuration": "kitchen.yaml"}
    )
    assert is_error
    assert text == "unavailable: secrets.yaml could not be parsed; validation output withheld"


@pytest.mark.parametrize(
    "frames",
    [[(StreamEvent.OUTPUT, "partial\n")], [(StreamEvent.RESULT, {"success": True})]],
    ids=["no_result", "no_code"],
)
async def test_validate_config_without_result_frame_is_an_error(
    mcp_client: Any, mcp_db: McpStubDeviceBuilder, frames: list[tuple[str, Any]]
) -> None:
    mcp_db.command_handlers["devices/validate"] = validate_stub(frames)
    is_error, text = await mcp_call(
        mcp_client, "validate_config", {"configuration": "kitchen.yaml"}
    )
    assert is_error
    assert text == "internal_error: Validation produced no result"


@pytest.mark.parametrize("swallow_cancel", [True, False], ids=["swallowed", "propagated"])
async def test_validate_config_times_out_and_keeps_the_tail(
    mcp_client: Any,
    mcp_db: McpStubDeviceBuilder,
    monkeypatch: pytest.MonkeyPatch,
    swallow_cancel: bool,
) -> None:
    monkeypatch.setattr("esphome_device_builder.api.mcp.tools.ESPHOME_CONFIG_TIMEOUT", 0.05)
    mcp_db.command_handlers["devices/validate"] = validate_stub(
        [(StreamEvent.OUTPUT, "started\n")], sleep=10, swallow_cancel=swallow_cancel
    )
    assert await mcp_call_json(
        mcp_client, "validate_config", {"configuration": "kitchen.yaml"}
    ) == {
        "success": False,
        "timed_out": True,
        "output": ["started"],
        "truncated": False,
    }


async def test_validate_config_reports_truncation(
    mcp_client: Any, mcp_db: McpStubDeviceBuilder
) -> None:
    frames: list[tuple[str, Any]] = [(StreamEvent.OUTPUT, f"{i}\n") for i in range(60)]
    frames.append((StreamEvent.RESULT, {"success": True, "code": 0}))
    mcp_db.command_handlers["devices/validate"] = validate_stub(frames)
    data = await mcp_call_json(mcp_client, "validate_config", {"configuration": "kitchen.yaml"})
    assert data["truncated"] is True
    assert data["output"][0] == "10"
    assert len(data["output"]) == 50

    more = await mcp_call_json(
        mcp_client, "validate_config", {"configuration": "kitchen.yaml", "tail_lines": 5000}
    )
    assert more["truncated"] is False
    assert len(more["output"]) == 60


# ---------------------------------------------------------------------------
# Firmware tools
# ---------------------------------------------------------------------------


async def test_compile_returns_job_id_and_status(
    mcp_client: Any, mcp_db: McpStubDeviceBuilder
) -> None:
    mcp_db.command_handlers["firmware/compile"] = AsyncMock(
        return_value=make_job("c1", status=JobStatus.QUEUED)
    )
    assert await mcp_call_json(mcp_client, "compile", {"configuration": "kitchen.yaml"}) == {
        "job_id": "c1",
        "status": "queued",
    }


async def test_install_reports_dependent_upload(
    mcp_client: Any, mcp_db: McpStubDeviceBuilder
) -> None:
    compile_job = make_job("c1", status=JobStatus.QUEUED)
    upload = make_job("u1", job_type=JobType.UPLOAD, status=JobStatus.QUEUED, depends_on="c1")
    install = AsyncMock(return_value=compile_job)
    mcp_db.command_handlers["firmware/install"] = install
    mcp_db.command_handlers["firmware/get_jobs"] = AsyncMock(return_value=[upload, compile_job])
    assert await mcp_call_json(
        mcp_client, "install", {"configuration": "kitchen.yaml", "port": "OTA"}
    ) == {"job_id": "c1", "status": "queued", "upload_job_id": "u1", "deferred": False}
    assert install.await_args.kwargs["port"] == "OTA"


async def test_install_deferred_has_no_upload(
    mcp_client: Any, mcp_db: McpStubDeviceBuilder
) -> None:
    deferred = make_job("c1", status=JobStatus.QUEUED, is_deferred_install=True)
    mcp_db.command_handlers["firmware/install"] = AsyncMock(return_value=deferred)
    mcp_db.command_handlers["firmware/get_jobs"] = AsyncMock(return_value=[deferred])
    assert await mcp_call_json(mcp_client, "install", {"configuration": "kitchen.yaml"}) == {
        "job_id": "c1",
        "status": "queued",
        "upload_job_id": None,
        "deferred": True,
    }


async def test_install_without_upload_and_not_deferred_is_an_error(
    mcp_client: Any, mcp_db: McpStubDeviceBuilder
) -> None:
    compile_job = make_job("c1", status=JobStatus.QUEUED)
    mcp_db.command_handlers["firmware/install"] = AsyncMock(return_value=compile_job)
    mcp_db.command_handlers["firmware/get_jobs"] = AsyncMock(return_value=[compile_job])
    is_error, text = await mcp_call(mcp_client, "install", {"configuration": "kitchen.yaml"})
    assert is_error
    assert text == "internal_error: Install chain for c1 has no upload job"


async def test_get_job_unknown_is_not_found(mcp_client: Any, mcp_db: McpStubDeviceBuilder) -> None:
    mcp_db.command_handlers["firmware/get_job"] = AsyncMock(return_value=None)
    assert await mcp_call(mcp_client, "get_job", {"job_id": "nope"}) == (
        True,
        "not_found: Job not found: nope",
    )


async def test_get_job_tails_ram_output_for_running_job(
    mcp_client: Any, mcp_db: McpStubDeviceBuilder
) -> None:
    job = make_job(output=[f"line {i}\n" for i in range(5)], progress=42)
    mcp_db.command_handlers["firmware/get_job"] = AsyncMock(return_value=job)
    data = await mcp_call_json(mcp_client, "get_job", {"job_id": "job1", "tail_lines": 2})
    assert data["output"] == ["line 3", "line 4"]
    assert data["status"] == "running"
    assert data["progress"] == 42
    assert data["queued_update_armed"] is False


async def test_get_job_reads_sidecar_for_terminal_job(
    mcp_client: Any, mcp_db: McpStubDeviceBuilder
) -> None:
    job = make_job(status=JobStatus.COMPLETED, exit_code=0)
    mcp_db.command_handlers["firmware/get_job"] = AsyncMock(return_value=job)
    with patch(
        "esphome_device_builder.controllers.firmware.follow.read_job_output",
        return_value=["a\n", "b\n", "c\n"],
    ) as read:
        data = await mcp_call_json(mcp_client, "get_job", {"job_id": "job1"})
    read.assert_called_once_with("job1")
    assert data["output"] == ["a", "b", "c"]
    assert data["exit_code"] == 0
    assert data["status"] == "completed"


async def test_get_job_zero_tail_skips_the_output_read(
    mcp_client: Any, mcp_db: McpStubDeviceBuilder
) -> None:
    job = make_job(status=JobStatus.COMPLETED, exit_code=0)
    mcp_db.command_handlers["firmware/get_job"] = AsyncMock(return_value=job)
    with patch("esphome_device_builder.controllers.firmware.follow.read_job_output") as read:
        data = await mcp_call_json(mcp_client, "get_job", {"job_id": "job1", "tail_lines": 0})
    read.assert_not_called()
    assert data["output"] == []


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("get_job", {"job_id": "job1", "tail_lines": -1}),
        ("validate_config", {"configuration": "kitchen.yaml", "tail_lines": -1}),
        ("search_components", {"query": "x", "limit": 0}),
        ("search_boards", {"query": "x", "limit": 0}),
    ],
)
async def test_negative_bounds_are_invalid_args(
    mcp_client: Any, mcp_db: McpStubDeviceBuilder, tool: str, arguments: dict[str, Any]
) -> None:
    is_error, text = await mcp_call(mcp_client, tool, arguments)
    assert is_error
    assert text.startswith("invalid_args: ")


async def test_tail_lines_and_limit_are_clamped(
    mcp_client: Any, mcp_db: McpStubDeviceBuilder
) -> None:
    mcp_db.command_handlers["firmware/get_job"] = AsyncMock(
        return_value=make_job(output=[f"{i}\n" for i in range(1500)])
    )
    data = await mcp_call_json(mcp_client, "get_job", {"job_id": "job1", "tail_lines": 5000})
    assert len(data["output"]) == 1000
    search = AsyncMock(return_value=PagedComponentsResponse(components=[]))
    mcp_db.command_handlers["components/get_components"] = search
    await mcp_call(mcp_client, "search_components", {"query": "x", "limit": 5000})
    assert search.await_args.kwargs["limit"] == 100


async def test_cancel_job(mcp_client: Any, mcp_db: McpStubDeviceBuilder) -> None:
    handler = AsyncMock(return_value=None)
    mcp_db.command_handlers["firmware/cancel"] = handler
    assert await mcp_call(mcp_client, "cancel_job", {"job_id": "job1"}) == (False, "Cancelled job1")
    assert handler.await_args.kwargs["job_id"] == "job1"


# ---------------------------------------------------------------------------
# Catalog tools
# ---------------------------------------------------------------------------


async def test_search_components_projects_index_rows(
    mcp_client: Any, mcp_db: McpStubDeviceBuilder
) -> None:
    entry = ComponentCatalogIndexEntry(
        id="sensor.dht",
        name="DHT",
        description="Temperature",
        category=ComponentCategory.SENSOR,
        docs_url="https://esphome.io/components/sensor/dht",
    )
    handler = AsyncMock(return_value=PagedComponentsResponse(components=[entry]))
    mcp_db.command_handlers["components/get_components"] = handler
    assert await mcp_call_json(mcp_client, "search_components", {"query": "dht"}) == [
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
    mcp_client: Any, mcp_catalog_db: McpStubDeviceBuilder
) -> None:
    body = await mcp_call_json(mcp_client, "get_component", {"component_id": "sensor.dht"})
    assert body["id"] == "sensor.dht"
    assert body["docs_url"].startswith("https://esphome.io/")
    entries = {entry["key"]: entry for entry in body["config_entries"]}
    assert {"pin", "model", "update_interval"} <= set(entries)
    assert "id" not in entries  # advanced
    assert {option["value"] for option in entries["model"]["options"]} >= {"DHT11", "DHT22"}
    assert not any("hidden" in entry or entry.get("advanced") for entry in entries.values())
    assert "null" not in json.dumps(body)


async def test_get_component_include_advanced(
    mcp_client: Any, mcp_catalog_db: McpStubDeviceBuilder
) -> None:
    slim = await mcp_call_json(mcp_client, "get_component", {"component_id": "sensor.dht"})
    full = await mcp_call_json(
        mcp_client, "get_component", {"component_id": "sensor.dht", "include_advanced": True}
    )
    assert "id" in {entry["key"] for entry in full["config_entries"]}
    assert len(json.dumps(full)) > len(json.dumps(slim))


async def test_get_component_unknown_is_not_found(
    mcp_client: Any, mcp_catalog_db: McpStubDeviceBuilder
) -> None:
    assert await mcp_call(mcp_client, "get_component", {"component_id": "sensor.nope"}) == (
        True,
        "not_found: Unknown component: sensor.nope",
    )


async def test_get_config_components_lists_catalog_rows_from_the_scan(
    mcp_client: Any, mcp_catalog_db: McpStubDeviceBuilder
) -> None:
    mcp_catalog_db.devices.get_by_configuration.return_value = make_device(
        "kitchen",
        component_ids=["substitutions", "esphome", "sensor", "sensor.dht", "sensor.hunter2"],
    )
    rows = await mcp_call_json(
        mcp_client, "get_config_components", {"configuration": "kitchen.yaml"}
    )
    mcp_catalog_db.devices.get_by_configuration.assert_called_with("kitchen.yaml")
    # Domain keys such as ``sensor`` have no catalog entry; a stray value is never echoed.
    assert [row["id"] for row in rows] == ["substitutions", "esphome", "sensor.dht"]
    assert rows[2]["name"]
    assert rows[2]["docs_url"].startswith("https://esphome.io/")
    assert "hunter2" not in json.dumps(rows)


@pytest.mark.parametrize(
    ("configuration", "code"),
    [("nope.yaml", "not_found"), ("../../etc/passwd", "invalid_args"), ("../x.yaml", "not_found")],
)
async def test_get_config_components_unknown_device_is_refused(
    mcp_client: Any, mcp_catalog_db: McpStubDeviceBuilder, configuration: str, code: str
) -> None:
    is_error, text = await mcp_call(
        mcp_client, "get_config_components", {"configuration": configuration}
    )
    assert is_error
    assert text.startswith(f"{code}: ")


async def test_get_config_components_without_catalog_is_unavailable(mcp_client: Any) -> None:
    is_error, text = await mcp_call(
        mcp_client, "get_config_components", {"configuration": "k.yaml"}
    )
    assert is_error
    assert text.startswith("unavailable: ")


# ---------------------------------------------------------------------------
# Device creation and secrets
# ---------------------------------------------------------------------------


async def test_search_boards_projects_index_rows(
    mcp_client: Any, mcp_db: McpStubDeviceBuilder, session_board_catalog: BoardCatalog
) -> None:
    handler = AsyncMock(side_effect=session_board_catalog.get_boards)
    mcp_db.command_handlers["boards/get_boards"] = handler
    rows = await mcp_call_json(mcp_client, "search_boards", {"query": "esp32dev", "limit": 5000})
    assert any(row["id"] == "esp32dev" for row in rows)
    assert all({"id", "name"} <= set(row) for row in rows)
    assert handler.await_args.kwargs["limit"] == 100


async def test_list_secret_names_returns_names_only(
    mcp_client: Any, mcp_db: McpStubDeviceBuilder
) -> None:
    mcp_db.command_handlers["config/get_secrets"] = AsyncMock(
        return_value=["wifi_password", "wifi_ssid"]
    )
    assert await mcp_call_json(mcp_client, "list_secret_names") == ["wifi_password", "wifi_ssid"]


async def test_set_secret_is_write_only(mcp_client: Any, mcp_db: McpStubDeviceBuilder) -> None:
    handler = AsyncMock(return_value={"created": True})
    mcp_db.command_handlers["config/set_secret"] = handler
    assert await mcp_call_json(
        mcp_client, "set_secret", {"name": "wifi_password", "value": "hunter2"}
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


async def test_create_device_never_passes_credentials(
    mcp_client: Any, mcp_db: McpStubDeviceBuilder
) -> None:
    handler = AsyncMock(return_value=WizardResponse(configuration="porch.yaml"))
    mcp_db.command_handlers["devices/create"] = handler
    assert await mcp_call_json(
        mcp_client,
        "create_device",
        {"name": "porch", "friendly_name": "Porch", "board_id": "esp32dev"},
    ) == {"configuration": "porch.yaml", "warning": None}
    assert "ssid" not in handler.await_args.kwargs
    assert "psk" not in handler.await_args.kwargs
    is_error, text = await mcp_call(mcp_client, "create_device", {"name": "x", "ssid": "net"})
    assert is_error
    assert "Unknown argument: ssid" in text


# ---------------------------------------------------------------------------
# Automations
# ---------------------------------------------------------------------------


async def test_get_automation_docs_example_resolves_against_the_catalog(
    mcp_client: Any, mcp_db: McpStubDeviceBuilder
) -> None:
    mcp_db.command_handlers["automations/get_bodies"] = AutomationsController(mcp_db).get_bodies  # type: ignore[arg-type]
    example = [{"type": "actions", "id": "light.turn_on"}]
    docs = await mcp_call_json(mcp_client, "get_automation_docs", {"refs": example})
    assert "config_entries" in docs["actions/light.turn_on"]
    for bad in ({"type": "action", "id": "light.turn_on"}, {"type": "actions"}, "actions/x"):
        is_error, text = await mcp_call(mcp_client, "get_automation_docs", {"refs": [bad]})
        assert is_error
        assert text.startswith("invalid_args: each ref needs a type of triggers, actions")


async def test_get_automation_docs_hides_advanced_fields_unless_asked(
    mcp_client: Any, mcp_db: McpStubDeviceBuilder
) -> None:
    body = {"id": "x", "config_entries": [{"key": "plain"}, {"key": "deep", "advanced": True}]}
    bodies = AsyncMock(return_value={"actions/x": body})
    mcp_db.command_handlers["automations/get_bodies"] = bodies
    refs = [{"type": "actions", "id": "x"}]
    docs = await mcp_call_json(mcp_client, "get_automation_docs", {"refs": refs})
    assert [e["key"] for e in docs["actions/x"]["config_entries"]] == ["plain"]
    docs = await mcp_call_json(
        mcp_client, "get_automation_docs", {"refs": refs, "include_advanced": True}
    )
    assert [e["key"] for e in docs["actions/x"]["config_entries"]] == ["plain", "deep"]
    assert "include_advanced" not in bodies.await_args.kwargs


async def test_automation_tools_wrap_the_automation_commands(
    mcp_client: Any, mcp_db: McpStubDeviceBuilder
) -> None:
    location = {"kind": "script", "index": 0}
    parsed = [
        {
            "location": location,
            "label": "blink",
            "raw_yaml": "script:\n",
            "automation": {"trigger": {"id": "script"}},
            "error": None,
        }
    ]
    mcp_db.command_handlers["automations/parse"] = AsyncMock(return_value=parsed)
    mcp_db.command_handlers["automations/get_available"] = AsyncMock(
        return_value={"triggers": ["on_boot"], "actions": ["light.turn_on"], "scripts": []}
    )
    bodies = AsyncMock(return_value={"actions/light.turn_on": {"id": "light.turn_on"}})
    mcp_db.command_handlers["automations/get_bodies"] = bodies
    mcp_db.command_handlers["devices/get_config"] = AsyncMock(return_value="a:\nb:\nc:\n")
    delete = AsyncMock(return_value={"yaml_diff": {"fromLine": 2, "toLine": 2, "replacement": ""}})
    mcp_db.command_handlers["automations/delete"] = delete
    save = AsyncMock(return_value=None)
    mcp_db.command_handlers["devices/update_config"] = save

    listed = await mcp_call_json(mcp_client, "list_automations", {"configuration": "kitchen.yaml"})
    assert listed == [{"location": location, "label": "blink", "raw_yaml": "script:\n"}]
    available = await mcp_call_json(
        mcp_client, "get_available_automations", {"configuration": "kitchen.yaml"}
    )
    assert available == {"triggers": ["on_boot"], "actions": ["light.turn_on"]}
    refs = [{"type": "actions", "id": "light.turn_on"}]
    docs = await mcp_call_json(mcp_client, "get_automation_docs", {"refs": refs})
    assert docs == {"actions/light.turn_on": {"id": "light.turn_on"}}
    assert bodies.await_args.kwargs["refs"] == refs
    assert await mcp_call(
        mcp_client, "delete_automation", {"configuration": "kitchen.yaml", "location": location}
    ) == (False, "Removed the automation and saved kitchen.yaml")
    assert delete.await_args.kwargs["location"] == location

    delete.return_value = {"yaml_diff": {"fromLine": 2, "toLine": 2, "replacement": "b:\n"}}
    is_error, text = await mcp_call(
        mcp_client, "delete_automation", {"configuration": "kitchen.yaml", "location": location}
    )
    assert is_error
    assert text == "internal_error: Delete produced no change"
    assert delete.await_args.kwargs["yaml"] == "a:\nb:\nc:\n"
    assert save.await_args.kwargs["content"] == "a:\nc:\n"
