"""Unit tests for the shared device error helpers in ``controllers/devices/helpers.py``."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from esphome_device_builder.controllers.devices.helpers import (
    raise_device_name_exists,
    raise_device_not_found,
    require_file_exists,
)
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode


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


def test_raise_device_name_exists_code_and_message() -> None:
    with pytest.raises(CommandError) as exc_info:
        raise_device_name_exists("living.yaml")
    assert exc_info.value.code is ErrorCode.INVALID_ARGS
    assert exc_info.value.message == "A device named living.yaml already exists"


def test_require_file_exists_passes_when_present(tmp_path: Path) -> None:
    target = tmp_path / "living.yaml"
    target.write_text("")
    require_file_exists(target, "living.yaml")


def test_require_file_exists_raises_when_absent(tmp_path: Path) -> None:
    with pytest.raises(CommandError, match=re.escape("File not found: living.yaml")) as exc_info:
        require_file_exists(tmp_path / "living.yaml", "living.yaml")
    assert exc_info.value.code is ErrorCode.NOT_FOUND


def test_require_file_exists_archived_prefix(tmp_path: Path) -> None:
    with pytest.raises(
        CommandError, match=re.escape("Archived file not found: living.yaml")
    ) as exc_info:
        require_file_exists(tmp_path / "living.yaml", "living.yaml", archived=True)
    assert exc_info.value.code is ErrorCode.NOT_FOUND
