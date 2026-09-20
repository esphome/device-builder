"""Coverage for the Device Builder MCP firmware tools."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from esphome_device_builder.api.mcp.tools import TOOLS
from esphome_device_builder.models import (
    JobStatus,
    JobType,
    PagedComponentsResponse,
)

from ..conftest import (
    McpStubDeviceBuilder,
    make_job,
)
from .conftest import (
    mcp_call,
    mcp_call_json,
)

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
    mcp_db.firmware.state.jobs.update({"u1": upload, "c1": compile_job})
    assert await mcp_call_json(
        mcp_client, "install", {"configuration": "kitchen.yaml", "port": "OTA"}
    ) == {"job_id": "c1", "status": "queued", "upload_job_id": "u1", "deferred": False}
    assert install.await_args.kwargs["port"] == "OTA"


async def test_install_deferred_has_no_upload(
    mcp_client: Any, mcp_db: McpStubDeviceBuilder
) -> None:
    deferred = make_job("c1", status=JobStatus.QUEUED, is_deferred_install=True)
    mcp_db.command_handlers["firmware/install"] = AsyncMock(return_value=deferred)
    mcp_db.firmware.state.jobs["c1"] = deferred
    assert await mcp_call_json(mcp_client, "install", {"configuration": "kitchen.yaml"}) == {
        "job_id": "c1",
        "status": "queued",
        "upload_job_id": None,
        "deferred": True,
    }


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


@pytest.mark.parametrize(
    ("tool", "arg", "bounds"),
    [
        ("get_job", "tail_lines", (0, 1000, 50)),
        ("validate_config", "tail_lines", (0, 1000, 50)),
        ("search_components", "limit", (1, 100, 20)),
        ("search_boards", "limit", (1, 100, 20)),
    ],
)
def test_tail_lines_and_limit_bounds_come_from_the_schema(
    tool: str, arg: str, bounds: tuple[int, int, int]
) -> None:
    prop = TOOLS[tool].schema["properties"][arg]
    assert (prop["minimum"], prop["maximum"], prop["default"]) == bounds


async def test_a_schema_default_is_forwarded_to_the_command(
    mcp_client: Any, mcp_db: McpStubDeviceBuilder
) -> None:
    search = AsyncMock(return_value=PagedComponentsResponse(components=[]))
    mcp_db.command_handlers["components/get_components"] = search
    await mcp_call(mcp_client, "search_components", {"query": "x"})
    assert search.await_args.kwargs["limit"] == 20


async def test_cancel_job(mcp_client: Any, mcp_db: McpStubDeviceBuilder) -> None:
    handler = AsyncMock(return_value=None)
    mcp_db.command_handlers["firmware/cancel"] = handler
    assert await mcp_call(mcp_client, "cancel_job", {"job_id": "job1"}) == (False, "Cancelled job1")
    assert handler.await_args.kwargs["job_id"] == "job1"
