"""Unit tests for the shared device error helpers in ``controllers/devices/helpers.py``."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, NoReturn, cast

import pytest

from esphome_device_builder.controllers.devices.helpers import (
    raise_device_name_exists,
    require_catalog,
    require_file_exists,
    require_unchanged,
    scanned_component_entries,
    write_new_file_exclusive,
)
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode

if TYPE_CHECKING:
    from collections.abc import Callable

    from esphome_device_builder.device_builder import DeviceBuilder


def test_require_catalog_raises_unavailable_when_unloaded() -> None:
    db = SimpleNamespace(components=None)
    with pytest.raises(CommandError) as excinfo:
        require_catalog(cast("DeviceBuilder", db))
    assert excinfo.value.code is ErrorCode.UNAVAILABLE

    db.components = object()
    assert require_catalog(cast("DeviceBuilder", db)) is db.components


def _scan_db(component_ids: list[str] | None) -> DeviceBuilder:
    """Build a db whose scan holds ``kitchen.yaml`` with *component_ids* (``None``: no devices)."""
    entries = {"esphome": object(), "sensor.dht": object()}
    device = SimpleNamespace(component_ids=component_ids)
    devices = SimpleNamespace(
        get_by_configuration=lambda name: device if name == "kitchen.yaml" else None
    )
    db = SimpleNamespace(
        components=SimpleNamespace(index_entry=entries.get),
        devices=None if component_ids is None else devices,
    )
    return cast("DeviceBuilder", db)


def test_scanned_component_entries_returns_the_catalogued_ids_in_scan_order() -> None:
    db = _scan_db(["esphome", "external.thing", "sensor.dht"])
    entries = scanned_component_entries(db, "kitchen.yaml")
    assert entries == [
        db.components.index_entry("esphome"),
        db.components.index_entry("sensor.dht"),
    ]


@pytest.mark.parametrize(
    ("component_ids", "configuration", "code"),
    [
        pytest.param(None, "kitchen.yaml", ErrorCode.UNAVAILABLE, id="devices_not_loaded"),
        pytest.param(["esphome"], "ghost.yaml", ErrorCode.NOT_FOUND, id="unknown_device"),
        pytest.param([], "kitchen.yaml", ErrorCode.UNAVAILABLE, id="scan_could_not_load_it"),
    ],
)
def test_scanned_component_entries_refuses(
    component_ids: list[str] | None, configuration: str, code: ErrorCode
) -> None:
    with pytest.raises(CommandError) as excinfo:
        scanned_component_entries(_scan_db(component_ids), configuration)
    assert excinfo.value.code is code


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


def test_require_unchanged_passes_the_text_it_started_from() -> None:
    require_unchanged("a: 1\n", "a: 1\n", "k.yaml")
    require_unchanged("a: 1\n", "a: 1", "k.yaml")


def test_require_unchanged_refuses_a_text_that_moved_on() -> None:
    with pytest.raises(CommandError) as excinfo:
        require_unchanged("a: 9\n", "a: 1\n", "k.yaml")
    assert excinfo.value.code is ErrorCode.PRECONDITION_FAILED
    assert excinfo.value.message.startswith("k.yaml differs from the expected text")
    assert "-a: 1\n+a: 9\n" in excinfo.value.message
