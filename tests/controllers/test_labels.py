"""Tests for ``controllers/labels.py`` — global label catalog CRUD.

The labels controller is a thin layer over the ``_labels`` key in
``.device-builder.json`` plus a pair of broadcast events. Coverage
focuses on:

* Validation rules (name length, color format, case-insensitive
  uniqueness) — the controller is the authoritative gate the
  frontend's input form mirrors, so a regression here lets bad
  data slip through to disk.
* Cascade-on-delete — labels are referenced by device entries; a
  delete must drop them from every assignment AND the catalog in
  one transaction so a concurrent reader can't see a dangling
  reference.
* Event emission shape — the frontend updates its label cache off
  these events.

Tests bypass-init the controller (``__new__`` + stubbed
``_db``) so the suite doesn't drag in the full ``DeviceBuilder``
lifecycle. The scanner reload path is exercised through a stub
that records its calls, mirroring how the production
``DevicesController.reload_configuration`` would feed the change
back into the live ``Device`` model.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from esphome_device_builder.controllers.config import (
    load_labels,
    save_labels,
    set_device_labels,
)
from esphome_device_builder.controllers.labels import LabelsController
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.event_bus import Event, EventBus
from esphome_device_builder.models import ErrorCode, EventType, Label


def _make_controller(
    config_dir: Path,
    *,
    reload_calls: list[str] | None = None,
) -> tuple[LabelsController, list[Event]]:
    """Bypass-init a ``LabelsController`` with a real bus + stubbed devices.

    Returns the controller and a live capture list of every event
    fanned out on the bus. The ``reload_calls`` list, when given,
    captures filenames passed to ``_db.devices.reload_configuration``
    so cascade tests can assert on the worklist.
    """
    bus = EventBus()
    captured: list[Event] = []
    for event_type in EventType:
        bus.add_listener(event_type, captured.append)

    controller = LabelsController.__new__(LabelsController)
    controller._db = MagicMock()
    controller._db.settings.config_dir = config_dir
    controller._db.bus = bus

    if reload_calls is None:
        controller._db.devices = None
    else:

        async def _reload(filename: str) -> bool:
            reload_calls.append(filename)
            return True

        controller._db.devices = MagicMock()
        controller._db.devices.reload_configuration = _reload

    return controller, captured


def _seed_device_yaml(tmp_path: Path, *filenames: str) -> None:
    """Write backing YAMLs so ``set_device_labels`` won't reject them as deleted."""
    for filename in filenames:
        (tmp_path / filename).write_text("esphome:\n  name: stub\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


async def test_create_label_persists_and_emits_event(tmp_path: Path) -> None:
    """Happy path: name + color saved, ``LABEL_CREATED`` fired with the label."""
    controller, captured = _make_controller(tmp_path)

    label = await controller.create_label(name="Kitchen", color="#FF0000")

    assert label.name == "Kitchen"
    # Color is normalised to lowercase on save so the on-disk shape is
    # canonical regardless of the user's input case.
    assert label.color == "#ff0000"
    assert len(label.id) == 32  # uuid4().hex

    persisted = await asyncio.to_thread(load_labels, tmp_path)
    assert persisted == [label]

    label_events = [e for e in captured if e.event_type == EventType.LABEL_CREATED]
    assert len(label_events) == 1
    assert label_events[0].data == {"label": label}


async def test_create_label_trims_whitespace(tmp_path: Path) -> None:
    """Surrounding whitespace is stripped before save AND uniqueness check."""
    controller, _ = _make_controller(tmp_path)

    label = await controller.create_label(name="  Kitchen  ")

    assert label.name == "Kitchen"


async def test_create_label_default_color_is_none(tmp_path: Path) -> None:
    """Omitting ``color`` lets the frontend pick a chip color."""
    controller, _ = _make_controller(tmp_path)

    label = await controller.create_label(name="Kitchen")

    assert label.color is None


@pytest.mark.parametrize("name", ["", "   ", "\t\n"])
async def test_create_label_rejects_empty_name(tmp_path: Path, name: str) -> None:
    """Empty / whitespace-only names raise ``INVALID_ARGS``."""
    controller, _ = _make_controller(tmp_path)

    with pytest.raises(CommandError) as exc_info:
        await controller.create_label(name=name)

    assert exc_info.value.code is ErrorCode.INVALID_ARGS


@pytest.mark.parametrize("name", [123, None, ["bad"], {"bad": True}])
async def test_create_label_rejects_non_string_name(tmp_path: Path, name: Any) -> None:
    """Non-string ``name`` payloads raise ``INVALID_ARGS``.

    The WS layer doesn't enforce arg types; the validator has to.
    Without this guard, ``name.strip()`` would crash with
    ``AttributeError`` and surface as a generic ``INTERNAL_ERROR``.
    """
    controller, _ = _make_controller(tmp_path)

    with pytest.raises(CommandError) as exc_info:
        await controller.create_label(name=name)

    assert exc_info.value.code is ErrorCode.INVALID_ARGS


async def test_create_label_rejects_overlong_name(tmp_path: Path) -> None:
    """Names longer than 50 chars (matches GitHub's cap) are rejected."""
    controller, _ = _make_controller(tmp_path)

    with pytest.raises(CommandError) as exc_info:
        await controller.create_label(name="x" * 51)

    assert exc_info.value.code is ErrorCode.INVALID_ARGS


@pytest.mark.parametrize("color", ["red", "#fff", "#GGGGGG", "ff0000", "#ff00ff00"])
async def test_create_label_rejects_invalid_color(tmp_path: Path, color: str) -> None:
    """Anything that isn't a 6-digit hex with leading ``#`` is rejected.

    The frontend's chip renderer trusts the saved value as a CSS
    color; loose parsing here would let ``"red"`` through, which
    works in CSS but breaks the color-picker round-trip.
    """
    controller, _ = _make_controller(tmp_path)

    with pytest.raises(CommandError) as exc_info:
        await controller.create_label(name="X", color=color)

    assert exc_info.value.code is ErrorCode.INVALID_ARGS


async def test_create_label_unique_name_case_insensitive(tmp_path: Path) -> None:
    """``"Kitchen"`` and ``"kitchen"`` are treated as the same name."""
    controller, _ = _make_controller(tmp_path)
    await controller.create_label(name="Kitchen")

    with pytest.raises(CommandError) as exc_info:
        await controller.create_label(name="kitchen")

    assert exc_info.value.code is ErrorCode.INVALID_ARGS
    # The original entry survives the rejected create.
    persisted = await asyncio.to_thread(load_labels, tmp_path)
    assert [lbl.name for lbl in persisted] == ["Kitchen"]


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


async def test_list_labels_returns_catalog_in_insertion_order(tmp_path: Path) -> None:
    """Catalog read returns labels in the order they were created."""
    await asyncio.to_thread(
        save_labels,
        tmp_path,
        [
            Label(id="a", name="Alpha"),
            Label(id="b", name="Bravo"),
            Label(id="c", name="Charlie"),
        ],
    )
    controller, _ = _make_controller(tmp_path)

    result = await controller.list_labels()

    assert [lbl.name for lbl in result] == ["Alpha", "Bravo", "Charlie"]


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


async def test_update_label_rename_preserves_id_and_color(tmp_path: Path) -> None:
    """Renaming changes the name but keeps the id and existing color.

    Devices reference labels by id — a rename must NOT mint a new
    id or every device assignment would be orphaned.
    """
    controller, captured = _make_controller(tmp_path)
    created = await controller.create_label(name="Kitchen", color="#ff0000")

    updated = await controller.update_label(label_id=created.id, name="Living Room")

    assert updated.id == created.id
    assert updated.name == "Living Room"
    assert updated.color == "#ff0000"  # untouched

    update_events = [e for e in captured if e.event_type == EventType.LABEL_UPDATED]
    assert len(update_events) == 1
    assert update_events[0].data == {"label": updated}


async def test_update_label_clear_color_via_explicit_null(tmp_path: Path) -> None:
    """``color=None`` clears the color; sentinel pattern distinguishes from "leave alone"."""
    controller, _ = _make_controller(tmp_path)
    created = await controller.create_label(name="Kitchen", color="#ff0000")

    updated = await controller.update_label(label_id=created.id, color=None)

    assert updated.name == created.name  # untouched (only color was passed)
    assert updated.color is None


async def test_update_label_no_changes_rejected(tmp_path: Path) -> None:
    """Passing neither name nor color is a user error.

    Without the guard, this would silently no-op and emit a
    ``LABEL_UPDATED`` event with unchanged data, confusing
    subscribers.
    """
    controller, _ = _make_controller(tmp_path)
    created = await controller.create_label(name="Kitchen")

    with pytest.raises(CommandError) as exc_info:
        await controller.update_label(label_id=created.id)

    assert exc_info.value.code is ErrorCode.INVALID_ARGS


async def test_update_label_unknown_id_returns_not_found(tmp_path: Path) -> None:
    """An id that isn't in the catalog raises ``NOT_FOUND``."""
    controller, _ = _make_controller(tmp_path)

    with pytest.raises(CommandError) as exc_info:
        await controller.update_label(label_id="ghost", name="X")

    assert exc_info.value.code is ErrorCode.NOT_FOUND


async def test_update_label_rename_blocked_by_other_label(tmp_path: Path) -> None:
    """Renaming to another label's name is rejected; existing entries unchanged."""
    controller, _ = _make_controller(tmp_path)
    a = await controller.create_label(name="Kitchen")
    b = await controller.create_label(name="Garage")

    with pytest.raises(CommandError) as exc_info:
        await controller.update_label(label_id=b.id, name="kitchen")  # case folded

    assert exc_info.value.code is ErrorCode.INVALID_ARGS
    # Self-rename to the same case-folded form is fine — it's
    # comparing the same id.
    same_case = await controller.update_label(label_id=a.id, name="KITCHEN")
    assert same_case.name == "KITCHEN"


# ---------------------------------------------------------------------------
# delete + cascade
# ---------------------------------------------------------------------------


async def test_delete_label_removes_from_catalog_and_emits_event(tmp_path: Path) -> None:
    """Delete drops the label and broadcasts ``LABEL_DELETED`` with the id."""
    controller, captured = _make_controller(tmp_path)
    created = await controller.create_label(name="Kitchen")

    result = await controller.delete_label(label_id=created.id)

    assert result == {"deleted": True}
    assert await asyncio.to_thread(load_labels, tmp_path) == []

    deleted_events = [e for e in captured if e.event_type == EventType.LABEL_DELETED]
    assert len(deleted_events) == 1
    assert deleted_events[0].data == {"label_id": created.id}


async def test_delete_label_unknown_id_raises_not_found(tmp_path: Path) -> None:
    """Deleting a label that isn't in the catalog raises ``NOT_FOUND``.

    Without the explicit check the call would silently succeed
    (cascade is a no-op when the id isn't on any device), and the
    frontend's "delete pressed twice" path would mask a real bug
    elsewhere.
    """
    controller, _ = _make_controller(tmp_path)

    with pytest.raises(CommandError) as exc_info:
        await controller.delete_label(label_id="ghost")

    assert exc_info.value.code is ErrorCode.NOT_FOUND


async def test_delete_label_cascades_through_assigned_devices(tmp_path: Path) -> None:
    """Devices that referenced the deleted label get reloaded.

    The reload is what makes the in-memory ``Device.labels`` list
    catch up with the trimmed sidecar — without it, the live model
    would lag the file until the next disk-driven scan.
    """
    reload_calls: list[str] = []
    controller, captured = _make_controller(tmp_path, reload_calls=reload_calls)

    a = await controller.create_label(name="Kitchen")
    b = await controller.create_label(name="Garage")
    _seed_device_yaml(tmp_path, "kitchen.yaml", "garage.yaml", "office.yaml")
    await asyncio.to_thread(set_device_labels, tmp_path, "kitchen.yaml", [a.id, b.id])
    await asyncio.to_thread(set_device_labels, tmp_path, "garage.yaml", [a.id])
    await asyncio.to_thread(set_device_labels, tmp_path, "office.yaml", [b.id])

    await controller.delete_label(label_id=a.id)

    assert sorted(reload_calls) == ["garage.yaml", "kitchen.yaml"]

    raw = json.loads((tmp_path / ".device-builder.json").read_bytes())
    assert raw["kitchen.yaml"]["labels"] == [b.id]
    assert "labels" not in raw["garage.yaml"]
    assert raw["office.yaml"]["labels"] == [b.id]

    deleted_events = [e for e in captured if e.event_type == EventType.LABEL_DELETED]
    assert len(deleted_events) == 1
    assert deleted_events[0].data == {"label_id": a.id}


async def test_delete_label_tolerates_devices_controller_absent(tmp_path: Path) -> None:
    """Cascade still completes when no DevicesController is wired.

    Pre-start lifecycle and isolated tests can drive the labels
    controller without a fully-initialised devices controller.
    The cascade should still scrub the sidecar — only the live
    Device reload is skipped.
    """
    controller, _ = _make_controller(tmp_path)  # devices=None

    created = await controller.create_label(name="Kitchen")
    _seed_device_yaml(tmp_path, "kitchen.yaml")
    await asyncio.to_thread(set_device_labels, tmp_path, "kitchen.yaml", [created.id])

    await controller.delete_label(label_id=created.id)

    raw = json.loads((tmp_path / ".device-builder.json").read_bytes())
    assert "labels" not in raw["kitchen.yaml"]


async def test_delete_label_swallows_per_device_reload_failures(tmp_path: Path) -> None:
    """A reload failure on one device doesn't stop the cascade.

    The cascade transaction has already committed by the time we
    reach the reload loop, so a transient scanner error (e.g.
    YAML disappeared between cascade write and reload) is logged
    and skipped — the next disk-driven scan picks up the cleaned
    state on its own. Pin: cascade keeps going even when reload
    raises.
    """
    bus = EventBus()
    captured: list[Event] = []
    for event_type in EventType:
        bus.add_listener(event_type, captured.append)

    controller = LabelsController.__new__(LabelsController)
    controller._db = MagicMock()
    controller._db.settings.config_dir = tmp_path
    controller._db.bus = bus

    async def _reload_raises(filename: str) -> bool:
        raise RuntimeError("scanner failed")

    controller._db.devices = MagicMock()
    controller._db.devices.reload_configuration = _reload_raises

    created = await controller.create_label(name="Kitchen")
    _seed_device_yaml(tmp_path, "kitchen.yaml")
    await asyncio.to_thread(set_device_labels, tmp_path, "kitchen.yaml", [created.id])

    # Cascade reaches the reload loop and swallows the exception;
    # ``LABEL_DELETED`` still fires.
    result = await controller.delete_label(label_id=created.id)
    assert result == {"deleted": True}

    deleted_events = [e for e in captured if e.event_type == EventType.LABEL_DELETED]
    assert len(deleted_events) == 1


async def test_delete_label_can_remove_corrupt_catalog_entry(tmp_path: Path) -> None:
    """A catalog entry that ``Label.from_dict`` would reject is still deletable.

    The existence check runs against the raw on-disk dict inside the
    cascade transaction, not against decoded ``Label`` instances.
    Without that, a hand-edited or partially-written entry would be
    impossible to clean up — ``load_labels`` would skip it (so
    ``NOT_FOUND``) but it would still occupy a slot in ``_labels``.
    """
    (tmp_path / ".device-builder.json").write_bytes(
        json.dumps(
            {
                "_labels": [
                    {"id": "corrupt"},  # missing ``name`` — Label.from_dict raises
                    {"id": "good", "name": "Good", "color": None},
                ]
            }
        ).encode()
    )
    controller, captured = _make_controller(tmp_path)

    result = await controller.delete_label(label_id="corrupt")

    assert result == {"deleted": True}
    raw = json.loads((tmp_path / ".device-builder.json").read_bytes())
    assert [entry["id"] for entry in raw["_labels"]] == ["good"]

    deleted_events = [e for e in captured if e.event_type == EventType.LABEL_DELETED]
    assert len(deleted_events) == 1


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------


async def test_persistence_round_trip_across_controller_instances(tmp_path: Path) -> None:
    """Labels created via one controller survive a fresh instance."""
    first, _ = _make_controller(tmp_path)
    a = await first.create_label(name="Alpha", color="#aabbcc")
    b = await first.create_label(name="Bravo")

    second, _ = _make_controller(tmp_path)
    result = await second.list_labels()

    assert result == [a, b]
