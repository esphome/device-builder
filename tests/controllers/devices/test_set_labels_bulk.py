"""Coverage for ``DevicesController.set_labels_bulk``."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from esphome_device_builder.controllers.config import save_labels
from esphome_device_builder.models import Label

from .conftest import (
    MakeControllerFactory,
    attach_reloading_scanner,
    make_label_test_device,
)


async def test_set_labels_bulk_applies_each_update(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Two updates each land on disk; both devices get reloaded."""
    await asyncio.to_thread(
        save_labels,
        tmp_path,
        [Label(id="lbl-a", name="Alpha"), Label(id="lbl-b", name="Bravo")],
    )

    controller = make_controller(tmp_path)
    scanner = attach_reloading_scanner(
        controller,
        tmp_path,
        [make_label_test_device("kitchen.yaml"), make_label_test_device("garage.yaml")],
    )

    result = await controller.set_labels_bulk(
        updates=[
            {"configuration": "kitchen.yaml", "label_ids": ["lbl-a"]},
            {"configuration": "garage.yaml", "label_ids": ["lbl-a", "lbl-b"]},
        ]
    )

    assert result == [
        {"configuration": "kitchen.yaml", "success": True},
        {"configuration": "garage.yaml", "success": True},
    ]

    raw = json.loads((tmp_path / ".device-builder.json").read_bytes())
    assert raw["kitchen.yaml"]["labels"] == ["lbl-a"]
    assert raw["garage.yaml"]["labels"] == ["lbl-a", "lbl-b"]

    assert ("reload", "kitchen.yaml") in scanner.calls
    assert ("reload", "garage.yaml") in scanner.calls


async def test_set_labels_bulk_reports_per_entry_failure(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Unknown label id fails its entry; valid entries still land."""
    await asyncio.to_thread(save_labels, tmp_path, [Label(id="lbl-a", name="Alpha")])

    controller = make_controller(tmp_path)
    attach_reloading_scanner(
        controller,
        tmp_path,
        [make_label_test_device("kitchen.yaml"), make_label_test_device("garage.yaml")],
    )

    result = await controller.set_labels_bulk(
        updates=[
            {"configuration": "kitchen.yaml", "label_ids": ["lbl-a"]},
            {"configuration": "garage.yaml", "label_ids": ["ghost"]},
        ]
    )

    by_config = {item["configuration"]: item for item in result}
    assert by_config["kitchen.yaml"]["success"] is True
    assert by_config["garage.yaml"]["success"] is False
    assert "ghost" in by_config["garage.yaml"]["error"]

    raw = json.loads((tmp_path / ".device-builder.json").read_bytes())
    assert raw["kitchen.yaml"]["labels"] == ["lbl-a"]
    assert "garage.yaml" not in raw


async def test_set_labels_bulk_preserves_input_order_with_duplicates(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Duplicate configurations in ``updates`` yield duplicate result rows; last write wins."""
    await asyncio.to_thread(
        save_labels,
        tmp_path,
        [Label(id="lbl-a", name="Alpha"), Label(id="lbl-b", name="Bravo")],
    )

    controller = make_controller(tmp_path)
    attach_reloading_scanner(controller, tmp_path, [make_label_test_device("kitchen.yaml")])

    result = await controller.set_labels_bulk(
        updates=[
            {"configuration": "kitchen.yaml", "label_ids": ["lbl-a"]},
            {"configuration": "kitchen.yaml", "label_ids": ["lbl-b"]},
        ]
    )

    assert [r["configuration"] for r in result] == ["kitchen.yaml", "kitchen.yaml"]
    assert all(r["success"] for r in result)

    raw = json.loads((tmp_path / ".device-builder.json").read_bytes())
    assert raw["kitchen.yaml"]["labels"] == ["lbl-b"]


async def test_set_labels_bulk_malformed_row_isolates_failure(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Each malformed row shape fails on its own row; valid rows still land."""
    await asyncio.to_thread(save_labels, tmp_path, [Label(id="lbl-a", name="Alpha")])

    controller = make_controller(tmp_path)
    attach_reloading_scanner(controller, tmp_path, [make_label_test_device("kitchen.yaml")])

    result = await controller.set_labels_bulk(
        updates=[
            {"label_ids": ["lbl-a"]},  # missing "configuration"
            {"configuration": "ghost.yaml"},  # missing "label_ids"
            {"configuration": "elsewhere.yaml", "label_ids": "lbl-a"},  # non-list label_ids
            None,  # type: ignore[list-item]  # non-dict row
            "oops",  # type: ignore[list-item]  # non-dict row
            {"configuration": "kitchen.yaml", "label_ids": ["lbl-a"]},
        ]
    )

    assert result[0] == {"configuration": "", "success": False, "error": result[0]["error"]}
    assert "configuration" in result[0]["error"]
    assert result[1]["success"] is False and "label_ids" in result[1]["error"]
    assert result[2]["success"] is False and "label_ids" in result[2]["error"]
    # Non-dict rows: configuration can't be extracted, so it surfaces as ""
    assert result[3] == {"configuration": "", "success": False, "error": result[3]["error"]}
    assert result[4] == {"configuration": "", "success": False, "error": result[4]["error"]}
    assert result[5] == {"configuration": "kitchen.yaml", "success": True}

    raw = json.loads((tmp_path / ".device-builder.json").read_bytes())
    assert raw["kitchen.yaml"]["labels"] == ["lbl-a"]


async def test_set_labels_bulk_empty_updates_returns_empty(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Empty ``updates`` returns ``[]`` with no per-device reload queued."""
    controller = make_controller(tmp_path)
    scanner = attach_reloading_scanner(
        controller, tmp_path, [make_label_test_device("kitchen.yaml")]
    )

    result = await controller.set_labels_bulk(updates=[])

    assert result == []
    assert all(call[0] != "reload" for call in scanner.calls)
