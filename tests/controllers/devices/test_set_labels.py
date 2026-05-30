"""End-to-end coverage for ``DevicesController.set_labels``.

The handler is a thin shim over the global label catalog plus a
per-device sidecar write — but the *sequence* matters: if the
catalog validation runs outside the metadata transaction, a
concurrent ``labels/delete`` can leave a dangling reference; if
the live ``Device`` model isn't refreshed after the sidecar
write, the next ``devices/list`` (which serves from the in-memory
scanner cache) returns stale labels.

Three contracts to pin:

1. Validation against the catalog runs inside the transaction so
   unknown ids reject without a partial write.
2. The sidecar write goes through ``set_device_labels`` (executor)
   so blockbuster doesn't fault on Linux CI.
3. After persistence the scanner's ``reload(filename)`` is
   awaited, so the in-memory ``Device.labels`` list reflects the
   trimmed sidecar without waiting on the next disk-driven scan.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from esphome_device_builder.controllers.config import (
    get_device_metadata,
    save_labels,
)
from esphome_device_builder.controllers.devices import DevicesController
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import Device, ErrorCode, Label

from .conftest import (
    MakeControllerFactory,
    ReloadingScanner,
    attach_reloading_scanner,
    make_label_test_device,
)


def _attach_reloading_scanner(
    controller: DevicesController, config_dir: Path, device: Device
) -> ReloadingScanner:
    """Adapt the multi-device hoisted helper for the existing single-device call sites."""
    return attach_reloading_scanner(controller, config_dir, [device])


def _make_device(filename: str = "kitchen.yaml", labels: list[str] | None = None) -> Device:
    return make_label_test_device(filename, labels)


async def test_set_labels_persists_and_reloads(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Happy path: labels land on disk, scanner reload fires, response is fresh."""
    await asyncio.to_thread(
        save_labels,
        tmp_path,
        [Label(id="lbl-a", name="Alpha"), Label(id="lbl-b", name="Bravo")],
    )

    controller = make_controller(tmp_path)
    scanner = _attach_reloading_scanner(controller, tmp_path, _make_device())

    result = await controller.set_labels(configuration="kitchen.yaml", label_ids=["lbl-a", "lbl-b"])

    # Sidecar reflects the assignment.
    raw = json.loads((tmp_path / ".device-builder.json").read_bytes())
    assert raw["kitchen.yaml"]["labels"] == ["lbl-a", "lbl-b"]

    # Scanner was asked to reload the just-written file.
    assert ("reload", "kitchen.yaml") in scanner.calls

    # The returned Device carries the freshly-loaded labels.
    assert isinstance(result, Device)
    assert result.configuration == "kitchen.yaml"
    assert result.labels == ["lbl-a", "lbl-b"]


async def test_set_labels_clear_drops_sidecar_key(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Passing ``[]`` removes the labels field entirely (no empty list left over)."""
    await asyncio.to_thread(save_labels, tmp_path, [Label(id="lbl-a", name="Alpha")])

    controller = make_controller(tmp_path)
    _attach_reloading_scanner(controller, tmp_path, _make_device())

    await controller.set_labels(configuration="kitchen.yaml", label_ids=["lbl-a"])
    result = await controller.set_labels(configuration="kitchen.yaml", label_ids=[])

    raw = json.loads((tmp_path / ".device-builder.json").read_bytes())
    assert "labels" not in raw["kitchen.yaml"]
    assert result.labels == []


async def test_set_labels_unknown_id_rejected_without_partial_write(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """An unknown id raises ``INVALID_ARGS`` and skips both write + reload.

    The catalog check runs inside the metadata transaction; if it
    fails, the transaction discards its mutations and the scanner
    never gets the reload signal. Pin both halves — a future
    refactor that ran reload eagerly would sometimes still fire on
    the failure path.
    """
    await asyncio.to_thread(save_labels, tmp_path, [Label(id="known", name="Known")])

    controller = make_controller(tmp_path)
    scanner = _attach_reloading_scanner(controller, tmp_path, _make_device())

    with pytest.raises(CommandError) as exc_info:
        await controller.set_labels(configuration="kitchen.yaml", label_ids=["known", "ghost"])

    assert exc_info.value.code is ErrorCode.INVALID_ARGS

    # Sidecar entry doesn't carry a half-applied list.
    meta = await asyncio.to_thread(get_device_metadata, tmp_path, "kitchen.yaml")
    assert "labels" not in meta

    # No reload was scheduled — the failure path bails before the
    # scanner is touched.
    assert ("reload", "kitchen.yaml") not in scanner.calls


async def test_set_labels_rejects_path_traversal(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """``rel_path`` chokepoint catches traversal attempts before any write.

    Reuses the same single-source path validation every other
    file-touching command does. Without the gate, a crafted
    ``configuration`` could persist labels into a sidecar entry
    that doesn't correspond to any real device.
    """
    controller = make_controller(tmp_path)

    # The production ``rel_path`` calls ``Path.resolve`` (sync
    # ``os.path.abspath`` under the hood) — fine in production but
    # blockbuster on Linux CI faults on it from an async test
    # context. Mimic the rejection on the same surface (a ``..``
    # segment is what the production guard ultimately catches via
    # ``relative_to``) without the blocking syscall.
    def _strict_rel_path(configuration: str) -> Path:
        if ".." in Path(configuration).parts or Path(configuration).is_absolute():
            raise CommandError(ErrorCode.INVALID_ARGS, "bad path")
        return tmp_path / configuration

    controller._db.settings.rel_path = _strict_rel_path

    with pytest.raises(CommandError) as exc_info:
        await controller.set_labels(configuration="../escape.yaml", label_ids=[])

    assert exc_info.value.code is ErrorCode.INVALID_ARGS


async def test_set_labels_rejects_non_list_label_ids(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """A non-list ``label_ids`` arg is a user error.

    The WS layer doesn't enforce schema on raw args; the controller
    has to. A regression that accepted a string would silently
    iterate over its characters and validate one-letter ids.
    """
    controller = make_controller(tmp_path)

    with pytest.raises(CommandError) as exc_info:
        await controller.set_labels(configuration="kitchen.yaml", label_ids="lbl-a")  # type: ignore[arg-type]

    assert exc_info.value.code is ErrorCode.INVALID_ARGS


async def test_set_labels_rejects_non_string_label_id(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """A non-string item inside ``label_ids`` raises ``INVALID_ARGS``.

    Silent skipping at the persistence layer would let a payload of
    all-bad types degrade to an effective ``[]`` (clear-all) write.
    The controller surfaces the validation error from
    ``set_device_labels`` cleanly.
    """
    controller = make_controller(tmp_path)
    _attach_reloading_scanner(controller, tmp_path, _make_device())
    await asyncio.to_thread(save_labels, tmp_path, [Label(id="lbl-a", name="Alpha")])

    with pytest.raises(CommandError) as exc_info:
        await controller.set_labels(
            configuration="kitchen.yaml",
            label_ids=["lbl-a", 42],  # type: ignore[list-item]
        )

    assert exc_info.value.code is ErrorCode.INVALID_ARGS

    # No partial / empty write happened.
    meta = await asyncio.to_thread(get_device_metadata, tmp_path, "kitchen.yaml")
    assert "labels" not in meta


async def test_set_labels_rejects_unknown_configuration(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """A ``configuration`` not in the scanner raises ``NOT_FOUND`` before any write.

    Without this check a typo'd configuration name would still pass
    ``rel_path`` (no traversal involved) and the persist layer would
    happily create a new sidecar entry — leaving an orphaned
    ``.device-builder.json`` row pinning labels to a non-existent
    device.
    """
    controller = make_controller(tmp_path)
    # Empty scanner devices list — no device matches.
    scanner = _attach_reloading_scanner(controller, tmp_path, _make_device("kitchen.yaml"))
    scanner.devices = []  # explicitly empty
    await asyncio.to_thread(save_labels, tmp_path, [Label(id="lbl-a", name="Alpha")])

    with pytest.raises(CommandError) as exc_info:
        await controller.set_labels(configuration="ghost.yaml", label_ids=["lbl-a"])

    assert exc_info.value.code is ErrorCode.NOT_FOUND

    # No sidecar entry created for the unknown configuration.
    meta = await asyncio.to_thread(get_device_metadata, tmp_path, "ghost.yaml")
    assert meta == {}
    # And no scanner reload was triggered.
    assert ("reload", "ghost.yaml") not in scanner.calls


async def test_reload_configuration_delegates_to_scanner(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """The public ``reload_configuration`` is a thin pass-through to the scanner.

    Pinned because it's the public seam the labels controller calls
    into during cascade-on-delete. A future refactor that renamed the
    scanner method or stopped awaiting it would silently break the
    cascade path; the assertion captures both the call shape and the
    return value.
    """
    controller = make_controller(tmp_path)
    # ``RecordingScanner.reload`` records every call and returns its
    # ``_reload_returns`` flag (defaults to ``True``).
    result = await controller.reload_configuration("kitchen.yaml")

    assert result is True
    assert ("reload", "kitchen.yaml") in controller._scanner.calls


async def test_set_labels_round_trips_through_metadata(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """The persisted sidecar entry is shaped exactly as ``get_device_metadata`` reads it.

    This catches a class of regression where the write side and
    the read side could drift on the labels field's encoding —
    e.g. one writing a tuple, the other expecting a list.
    """
    await asyncio.to_thread(save_labels, tmp_path, [Label(id="lbl-a", name="Alpha")])

    controller = make_controller(tmp_path)
    _attach_reloading_scanner(controller, tmp_path, _make_device())

    await controller.set_labels(configuration="kitchen.yaml", label_ids=["lbl-a"])

    md = await asyncio.to_thread(get_device_metadata, tmp_path, "kitchen.yaml")
    assert md["labels"] == ["lbl-a"]
