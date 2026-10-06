"""Tests for the device config read helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest

from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.device_config import (
    raise_device_not_found,
    read_device_config,
    read_device_config_async,
)
from esphome_device_builder.models import ErrorCode

if TYPE_CHECKING:
    from esphome_device_builder.controllers.config.settings import DashboardSettings


def test_raise_device_not_found_code_and_message() -> None:
    with pytest.raises(CommandError) as exc_info:
        raise_device_not_found("living.yaml")
    assert exc_info.value.code is ErrorCode.NOT_FOUND
    assert exc_info.value.message == "Device 'living.yaml' not found"


def test_raise_device_not_found_chains_cause() -> None:
    cause = FileNotFoundError("gone")
    with pytest.raises(CommandError) as exc_info:
        raise_device_not_found("living.yaml", from_exc=cause)
    assert exc_info.value.__cause__ is cause


def test_read_device_config_returns_the_text_or_not_found(tmp_path: Path) -> None:
    path = tmp_path / "kitchen.yaml"
    path.write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    assert read_device_config(path, "kitchen.yaml") == "esphome:\n  name: kitchen\n"

    with pytest.raises(CommandError) as excinfo:
        read_device_config(tmp_path / "ghost.yaml", "ghost.yaml")
    assert excinfo.value.code is ErrorCode.NOT_FOUND
    assert "ghost.yaml" in excinfo.value.message


def test_read_device_config_answers_not_found_when_the_file_vanishes_mid_read(
    tmp_path: Path,
) -> None:
    path = tmp_path / "kitchen.yaml"
    path.write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    vanished = FileNotFoundError(path)

    with (
        patch.object(Path, "read_text", side_effect=vanished),
        pytest.raises(CommandError) as excinfo,
    ):
        read_device_config(path, "kitchen.yaml")

    assert excinfo.value.code is ErrorCode.NOT_FOUND
    assert excinfo.value.__cause__ is vanished


async def test_read_device_config_async_resolves_through_the_settings(tmp_path: Path) -> None:
    (tmp_path / "kitchen.yaml").write_text("esphome:\n", encoding="utf-8")
    settings = cast("DashboardSettings", SimpleNamespace(rel_path=lambda name: tmp_path / name))

    assert await read_device_config_async(settings, "kitchen.yaml") == "esphome:\n"
    with pytest.raises(CommandError) as excinfo:
        await read_device_config_async(settings, "ghost.yaml")
    assert excinfo.value.code is ErrorCode.NOT_FOUND
