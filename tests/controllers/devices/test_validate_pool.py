"""``devices/validate`` queues behind a bounded pool and refuses instead of hanging."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from esphome_device_builder.controllers.devices import validate
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode


def _controller(tmp_path: Path, gate: asyncio.Event) -> MagicMock:
    controller = MagicMock()
    controller._db.settings.rel_path = lambda name: tmp_path / name
    controller.state.esphome_cmd = ["esphome"]

    async def stream(*_args: object, **_kwargs: object) -> None:
        await gate.wait()

    controller._stream_subprocess = stream
    return controller


async def test_fourth_validate_is_unavailable_while_the_pool_is_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(validate, "_QUEUE_TIMEOUT", 0.01)
    gate = asyncio.Event()
    controller = _controller(tmp_path, gate)
    kwargs = {"show_secrets": False, "client": MagicMock(), "message_id": "m"}
    running = [
        asyncio.create_task(validate.validate_config(controller, configuration="a.yaml", **kwargs))
        for _ in range(validate._MAX_CONCURRENT_VALIDATES)
    ]
    await asyncio.sleep(0)
    with pytest.raises(CommandError) as excinfo:
        await validate.validate_config(controller, configuration="d.yaml", **kwargs)
    assert excinfo.value.code is ErrorCode.UNAVAILABLE
    gate.set()
    await asyncio.gather(*running)
    await validate.validate_config(controller, configuration="e.yaml", **kwargs)
