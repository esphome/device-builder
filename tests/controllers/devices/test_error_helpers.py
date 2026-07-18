"""Unit tests for the shared device error helpers in ``controllers/devices/helpers.py``."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, cast

import pytest

from esphome_device_builder.controllers.devices.helpers import (
    raise_device_name_exists,
    raise_device_not_found,
    require_file_exists,
    write_new_file_exclusive,
)
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode

if TYPE_CHECKING:
    from collections.abc import Callable


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


async def test_write_new_file_exclusive_writes_and_skips_on_exists(tmp_path: Path) -> None:
    """A fresh path is written; ``on_exists`` is never consulted."""
    target = tmp_path / "kitchen.yaml"

    def _fail(exc: BaseException) -> NoReturn:
        raise AssertionError("on_exists must not run for a fresh path")

    await write_new_file_exclusive(target, "esphome:\n", on_exists=_fail)

    assert target.read_text(encoding="utf-8") == "esphome:\n"


async def test_write_new_file_exclusive_delegates_existing_to_on_exists(tmp_path: Path) -> None:
    """An existing target raises the caller's typed error and keeps its content."""
    target = tmp_path / "kitchen.yaml"
    target.write_text("original", encoding="utf-8")

    def _raise(exc: BaseException) -> NoReturn:
        raise_device_name_exists("kitchen.yaml", from_exc=exc)

    with pytest.raises(CommandError) as exc_info:
        await write_new_file_exclusive(target, "clobber", on_exists=_raise)

    assert exc_info.value.code is ErrorCode.INVALID_ARGS
    assert target.read_text(encoding="utf-8") == "original"


async def test_write_new_file_exclusive_reraises_when_on_exists_returns(tmp_path: Path) -> None:
    """A contract-violating ``on_exists`` that returns can't swallow the failure."""
    target = tmp_path / "kitchen.yaml"
    target.write_text("original", encoding="utf-8")
    seen: list[BaseException] = []

    with pytest.raises(FileExistsError):
        await write_new_file_exclusive(
            target,
            "clobber",
            on_exists=cast("Callable[[BaseException], NoReturn]", seen.append),
        )

    assert len(seen) == 1
    assert target.read_text(encoding="utf-8") == "original"
