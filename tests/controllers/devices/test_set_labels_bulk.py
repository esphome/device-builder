"""Coverage for ``DevicesController.set_labels_bulk`` (#928).

Mirrors ``test_set_labels.py``'s per-device coverage but for the
multi-device entry. The contract:

1. All-success path: each entry in ``updates`` lands its sidecar
   write, reload fires per device, response is one
   ``{configuration, success: True}`` per entry.
2. Mixed path: a per-entry failure (unknown label id) returns
   ``{success: False, error: ...}`` for that entry; the valid
   entries still land.
3. Empty ``updates`` returns ``[]`` with no per-device ``reload``
   queued (the trailing ``scan()`` from the bulk loop still fires,
   matching ``delete_bulk`` / ``archive_bulk``).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from esphome_device_builder.controllers.config import save_labels
from esphome_device_builder.controllers.devices import DevicesController
from esphome_device_builder.helpers.device_yaml import configuration_stem
from esphome_device_builder.models import Device, Label
from tests.conftest import make_device

from .conftest import MakeControllerFactory
from .test_set_labels import _ReloadingScanner


def _make_device(filename: str, labels: list[str] | None = None) -> Device:
    name = configuration_stem(filename)
    return make_device(
        name=name,
        friendly_name=name,
        configuration=filename,
        address="",
        labels=list(labels or []),
    )


def _attach_multi_scanner(
    controller: DevicesController, config_dir: Path, devices: list[Device]
) -> _ReloadingScanner:
    """Like ``test_set_labels._attach_reloading_scanner`` but seeds N devices."""
    scanner = _ReloadingScanner(config_dir, devices[0])
    scanner.devices = list(devices)
    controller._scanner = scanner
    return scanner


@pytest.mark.asyncio
async def test_set_labels_bulk_applies_each_update(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """All entries succeed: per-device sidecar writes + reloads, success=True."""
    await asyncio.to_thread(
        save_labels,
        tmp_path,
        [Label(id="lbl-a", name="Alpha"), Label(id="lbl-b", name="Bravo")],
    )

    controller = make_controller(tmp_path)
    scanner = _attach_multi_scanner(
        controller,
        tmp_path,
        [_make_device("kitchen.yaml"), _make_device("garage.yaml")],
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


@pytest.mark.asyncio
async def test_set_labels_bulk_reports_per_entry_failure(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """An unknown label id fails its entry without blocking the rest.

    The valid entry's sidecar still lands. Pin per-entry error
    isolation so a future refactor that short-circuited the whole
    bulk call on first failure would surface.
    """
    await asyncio.to_thread(save_labels, tmp_path, [Label(id="lbl-a", name="Alpha")])

    controller = make_controller(tmp_path)
    _attach_multi_scanner(
        controller,
        tmp_path,
        [_make_device("kitchen.yaml"), _make_device("garage.yaml")],
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
    # The failing entry never touched the sidecar (no orphan).
    assert "garage.yaml" not in raw


@pytest.mark.asyncio
async def test_set_labels_bulk_preserves_input_order_with_duplicates(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Duplicate configurations in ``updates`` yield duplicate result rows.

    A frontend bug (or a power-user re-applying labels via repeated
    rows) shouldn't silently drop entries. The contract is one result
    per input row, in input order; the last write per configuration
    wins on disk because the rows are applied sequentially.
    """
    await asyncio.to_thread(
        save_labels,
        tmp_path,
        [Label(id="lbl-a", name="Alpha"), Label(id="lbl-b", name="Bravo")],
    )

    controller = make_controller(tmp_path)
    _attach_multi_scanner(controller, tmp_path, [_make_device("kitchen.yaml")])

    result = await controller.set_labels_bulk(
        updates=[
            {"configuration": "kitchen.yaml", "label_ids": ["lbl-a"]},
            {"configuration": "kitchen.yaml", "label_ids": ["lbl-b"]},
        ]
    )

    # Two result rows for the two input rows, in order.
    assert [r["configuration"] for r in result] == ["kitchen.yaml", "kitchen.yaml"]
    assert all(r["success"] for r in result)

    # Last write wins on disk.
    raw = json.loads((tmp_path / ".device-builder.json").read_bytes())
    assert raw["kitchen.yaml"]["labels"] == ["lbl-b"]


@pytest.mark.asyncio
async def test_set_labels_bulk_malformed_row_isolates_failure(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """A row missing required keys fails for that row only, not the rest."""
    await asyncio.to_thread(save_labels, tmp_path, [Label(id="lbl-a", name="Alpha")])

    controller = make_controller(tmp_path)
    _attach_multi_scanner(controller, tmp_path, [_make_device("kitchen.yaml")])

    result = await controller.set_labels_bulk(
        updates=[
            {"label_ids": ["lbl-a"]},  # missing "configuration"
            {"configuration": "ghost.yaml"},  # missing "label_ids"
            {"configuration": "elsewhere.yaml", "label_ids": "lbl-a"},  # non-list label_ids
            {"configuration": "kitchen.yaml", "label_ids": ["lbl-a"]},
        ]
    )

    assert result[0]["success"] is False
    assert "configuration" in result[0]["error"]
    assert result[1]["success"] is False
    assert "label_ids" in result[1]["error"]
    assert result[2]["success"] is False
    assert "label_ids" in result[2]["error"]
    assert result[3] == {"configuration": "kitchen.yaml", "success": True}

    raw = json.loads((tmp_path / ".device-builder.json").read_bytes())
    assert raw["kitchen.yaml"]["labels"] == ["lbl-a"]


@pytest.mark.asyncio
async def test_set_labels_bulk_empty_updates_returns_empty(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Empty ``updates`` returns ``[]`` and never reloads any device.

    ``run_bulk_per_device`` always fires its trailing ``scan()``; the
    invariant that matters here is that no per-device ``reload`` was
    queued.
    """
    controller = make_controller(tmp_path)
    scanner = _attach_multi_scanner(controller, tmp_path, [_make_device("kitchen.yaml")])

    result = await controller.set_labels_bulk(updates=[])

    assert result == []
    assert all(call[0] != "reload" for call in scanner.calls)
