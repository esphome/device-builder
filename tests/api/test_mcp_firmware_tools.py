"""Coverage for the Device Builder MCP firmware tools."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

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
    assert text == (
        "internal_error: Compile job c1 is queued but its install chain has no upload job; "
        "poll it with get_job instead of retrying install"
    )


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


async def test_get_job_zero_tail_still_reports_withheld_output(
    mcp_client: Any, mcp_db: McpStubDeviceBuilder
) -> None:
    job = make_job(status=JobStatus.COMPLETED, exit_code=0)
    mcp_db.command_handlers["firmware/get_job"] = AsyncMock(return_value=job)
    with patch(
        "esphome_device_builder.controllers.firmware.follow.read_job_output",
        return_value=["built\n"],
    ):
        data = await mcp_call_json(mcp_client, "get_job", {"job_id": "job1", "tail_lines": 0})
    assert data["output"] == []
    assert data["truncated"] is True
    assert data["output_available"] is True


async def test_get_job_redacts_concealed_values(
    mcp_client: Any, mcp_db: McpStubDeviceBuilder
) -> None:
    job = make_job(output=["  password: \x1b[8mhunter2secret\x1b[28m\n"])
    mcp_db.command_handlers["firmware/get_job"] = AsyncMock(return_value=job)
    data = await mcp_call_json(mcp_client, "get_job", {"job_id": "job1"})
    assert data["output"] == ["  password: <removed>"]


async def test_get_job_flags_an_unreadable_log(
    mcp_client: Any, mcp_db: McpStubDeviceBuilder
) -> None:
    job = make_job(status=JobStatus.COMPLETED, exit_code=0)
    mcp_db.command_handlers["firmware/get_job"] = AsyncMock(return_value=job)
    with patch(
        "esphome_device_builder.controllers.firmware.follow.read_job_output", return_value=None
    ):
        data = await mcp_call_json(mcp_client, "get_job", {"job_id": "job1"})
    assert data["output"] == []
    assert data["output_available"] is False


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
