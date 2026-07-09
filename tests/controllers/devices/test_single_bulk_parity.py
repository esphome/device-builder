"""Single-vs-bulk parity for the devices delete/archive/set_labels command pairs."""

from __future__ import annotations

from pathlib import Path

import pytest

from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode

from .conftest import MakeControllerFactory, attach_reloading_scanner


async def test_delete_parity_missing_yaml(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Single raises NOT_FOUND; the bulk row carries the same message."""
    single = make_controller(tmp_path)
    bulk = make_controller(tmp_path)

    with pytest.raises(CommandError) as exc_info:
        await single.delete_device(configuration="ghost.yaml")
    rows = await bulk.delete_bulk(configurations=["ghost.yaml"])

    assert exc_info.value.code == ErrorCode.NOT_FOUND
    assert rows == [
        {"configuration": "ghost.yaml", "success": False, "error": exc_info.value.message}
    ]


async def test_delete_parity_existing_yaml(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    controller = make_controller(tmp_path)
    for name in ("kitchen.yaml", "office.yaml"):
        (tmp_path / name).write_text("esphome:\n  name: x\n")

    await controller.delete_device(configuration="kitchen.yaml")
    rows = await controller.delete_bulk(configurations=["office.yaml"])

    assert rows == [{"configuration": "office.yaml", "success": True}]
    assert not (tmp_path / "kitchen.yaml").exists()
    assert not (tmp_path / "office.yaml").exists()


async def test_archive_parity_traversal_name(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Single rejects INVALID_ARGS; the bulk row carries the same message."""
    single = make_controller(tmp_path)
    bulk = make_controller(tmp_path)
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: x\n")

    with pytest.raises(CommandError) as exc_info:
        await single.archive_device(configuration="../evil.yaml")
    rows = await bulk.archive_bulk(configurations=["../evil.yaml", "kitchen.yaml"])

    assert exc_info.value.code == ErrorCode.INVALID_ARGS
    assert rows[0] == {
        "configuration": "../evil.yaml",
        "success": False,
        "error": exc_info.value.message,
    }
    # Unlike the firmware bulk verbs' atomic batch validation, devices
    # bulk isolates the bad row — the good config still archives.
    assert rows[1] == {"configuration": "kitchen.yaml", "success": True}
    assert (tmp_path / "archive" / "kitchen.yaml").exists()


async def test_archive_parity_missing_yaml(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    single = make_controller(tmp_path)
    bulk = make_controller(tmp_path)

    with pytest.raises(CommandError) as exc_info:
        await single.archive_device(configuration="ghost.yaml")
    rows = await bulk.archive_bulk(configurations=["ghost.yaml"])

    assert exc_info.value.code == ErrorCode.NOT_FOUND
    assert rows == [
        {"configuration": "ghost.yaml", "success": False, "error": exc_info.value.message}
    ]


async def test_set_labels_parity_missing_device(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    single = make_controller(tmp_path)
    bulk = make_controller(tmp_path)
    attach_reloading_scanner(single, tmp_path, [])
    attach_reloading_scanner(bulk, tmp_path, [])

    with pytest.raises(CommandError) as exc_info:
        await single.set_labels(configuration="ghost.yaml", label_ids=[])
    rows = await bulk.set_labels_bulk(updates=[{"configuration": "ghost.yaml", "label_ids": []}])

    assert exc_info.value.code == ErrorCode.NOT_FOUND
    assert rows == [
        {"configuration": "ghost.yaml", "success": False, "error": exc_info.value.message}
    ]
