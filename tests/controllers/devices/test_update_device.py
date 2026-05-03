"""End-to-end coverage for ``DevicesController.update_device``.

The handler updates the device-builder metadata sidecar
(``.device-builder.json``) — not the YAML file — and returns an
``UpdateDeviceResponse`` populated from the freshly-written entry.
Three contracts to pin:

1. The persist call lands on disk via ``set_device_metadata``
   (routed through the executor so blockbuster doesn't fault).
2. The response carries the values that just landed, with the
   sidecar acting as the source of truth (so the next
   ``devices/list`` sees the same values).
3. ``friendly_name`` defaults to the device's name when the
   metadata doesn't carry one — that way the dashboard never
   renders an empty label even on a fresh device.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from esphome_device_builder.controllers.config import (
    get_device_metadata,
    set_device_metadata,
)

from .conftest import MakeControllerFactory


@pytest.mark.asyncio
async def test_update_device_writes_full_metadata(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """The three sidecar-stored fields land on disk and round-trip back.

    ``name`` is the entry's filename key (``<name>.yaml``), not a
    sidecar field; ``friendly_name`` / ``comment`` / ``board_id``
    are the three values actually written into
    ``.device-builder.json``. Pin the persist + read-back contract
    end-to-end. The previous shape was a tight feedback loop where
    the response was built from the *input* args; that drifted out
    of sync with the on-disk state when a future call with partial
    fields would inherit stale values. Reading from the sidecar
    after the write is what keeps the response authoritative.
    """
    controller = make_controller(tmp_path)

    response = await controller.update_device(
        name="kitchen",
        friendly_name="Kitchen Sensor",
        comment="On the wall by the toaster",
        board_id="esp32-c3-devkitm-1",
    )

    # Response carries the persisted values.
    assert response.name == "kitchen"
    assert response.friendly_name == "Kitchen Sensor"
    assert response.comment == "On the wall by the toaster"
    assert response.board_id == "esp32-c3-devkitm-1"

    # Sidecar on disk matches.
    meta = await asyncio.to_thread(get_device_metadata, tmp_path, "kitchen.yaml")
    assert meta["friendly_name"] == "Kitchen Sensor"
    assert meta["comment"] == "On the wall by the toaster"
    assert meta["board_id"] == "esp32-c3-devkitm-1"


@pytest.mark.asyncio
async def test_update_device_partial_keeps_unrelated_fields(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A partial update only changes the fields the caller passed.

    ``set_device_metadata`` defaults each kwarg to ``None`` and
    only writes when the caller passes a non-None value. A
    refactor that flipped that contract would silently wipe every
    other field on every update. Pin the partial-update path so
    "the user only changed the comment" doesn't lose their
    ``board_id`` mapping.
    """
    controller = make_controller(tmp_path)

    # Seed an existing entry with all three sidecar-stored fields.
    await asyncio.to_thread(
        set_device_metadata,
        tmp_path,
        "kitchen.yaml",
        board_id="esp32-c3-devkitm-1",
        friendly_name="Kitchen Sensor",
        comment="Old comment",
    )

    response = await controller.update_device(name="kitchen", comment="New comment")

    # Only the comment changed; the other identity fields survive.
    assert response.comment == "New comment"
    assert response.friendly_name == "Kitchen Sensor"
    assert response.board_id == "esp32-c3-devkitm-1"


@pytest.mark.asyncio
async def test_update_device_falls_back_to_name_for_missing_friendly_name(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """``friendly_name`` defaults to the device's ``name`` when sidecar has none.

    A fresh device with no metadata yet shouldn't render with an
    empty label in the UI. The handler's ``meta.get("friendly_name", name)``
    is the fallback that keeps the dashboard's drawer / list view
    populated even before the user has set anything.
    """
    controller = make_controller(tmp_path)

    response = await controller.update_device(name="kitchen", board_id="esp32-c3-devkitm-1")

    # No friendly_name passed, no sidecar entry → falls back to ``name``.
    assert response.friendly_name == "kitchen"
    # ``comment`` stays None when never set.
    assert response.comment is None
    # ``board_id`` is what we just wrote.
    assert response.board_id == "esp32-c3-devkitm-1"


@pytest.mark.asyncio
async def test_update_device_persists_via_executor(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """The blocking ``set_device_metadata`` runs in the executor.

    Pin the executor route by reading the file inside the same
    test loop after the call returns. Without ``run_in_executor``
    blockbuster would fault on the synchronous ``tempfile.mkstemp``
    inside ``metadata_transaction``; the test passing on Linux CI
    is what proves the route stays correct.
    """
    controller = make_controller(tmp_path)

    await controller.update_device(name="kitchen", friendly_name="Kitchen")

    # The atomic-replace landed a real JSON file on disk.
    sidecar = tmp_path / ".device-builder.json"
    assert sidecar.exists()
