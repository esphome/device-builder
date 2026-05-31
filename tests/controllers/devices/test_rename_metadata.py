"""
Device metadata survives a rename.

``esphome rename`` swaps the YAML filename, and the device's metadata
is keyed by filename across two stores: the shared sidecar
(``.device-builder.json`` — identity: board_id / comment / labels) and
the data-dir store (volatile: config hashes / ip). Without an explicit
migration on rename completion the old entries orphan and the renamed
device loads with none — silently dropping the user's labels and
comment. These tests pin the migration that the RENAME job-completion
hook performs before the rescan rebuilds the device.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from esphome_device_builder.controllers.config import (
    get_device_metadata,
    rename_device_metadata,
    set_device_metadata,
)
from esphome_device_builder.controllers.devices import firmware_sync
from esphome_device_builder.helpers.event_bus import Event
from esphome_device_builder.models import (
    EventType,
    FirmwareJob,
    JobStatus,
    JobType,
)

from .conftest import MakeControllerFactory


def test_rename_device_metadata_carries_identity_fields(tmp_path: Path) -> None:
    """The transactional move drops the old key and lands every field on the new one."""
    set_device_metadata(
        tmp_path,
        "kitchen.yaml",
        board_id="esp32dev",
        comment="downstairs",
        labels=["a", "b"],
    )

    rename_device_metadata(tmp_path, "kitchen.yaml", "livingroom.yaml")

    assert get_device_metadata(tmp_path, "kitchen.yaml") == {}
    moved = get_device_metadata(tmp_path, "livingroom.yaml")
    assert moved.get("board_id") == "esp32dev"
    assert moved.get("comment") == "downstairs"
    assert moved.get("labels") == ["a", "b"]


def test_rename_device_metadata_existing_target_fields_win(tmp_path: Path) -> None:
    """A scan-derived entry already under the new name isn't clobbered."""
    set_device_metadata(tmp_path, "kitchen.yaml", board_id="esp32dev", comment="old")
    set_device_metadata(tmp_path, "livingroom.yaml", comment="fresh")

    rename_device_metadata(tmp_path, "kitchen.yaml", "livingroom.yaml")

    moved = get_device_metadata(tmp_path, "livingroom.yaml")
    # Old contributes board_id; the pre-existing comment wins.
    assert moved.get("board_id") == "esp32dev"
    assert moved.get("comment") == "fresh"


def test_rename_device_metadata_same_name_is_noop(tmp_path: Path) -> None:
    """Old == new leaves the entry untouched."""
    set_device_metadata(tmp_path, "kitchen.yaml", comment="keep")

    rename_device_metadata(tmp_path, "kitchen.yaml", "kitchen.yaml")

    assert get_device_metadata(tmp_path, "kitchen.yaml").get("comment") == "keep"


def test_rename_device_metadata_missing_old_entry_is_noop(tmp_path: Path) -> None:
    """No old entry leaves the target untouched (early-return guard)."""
    set_device_metadata(tmp_path, "livingroom.yaml", comment="fresh")

    rename_device_metadata(tmp_path, "kitchen.yaml", "livingroom.yaml")

    assert get_device_metadata(tmp_path, "kitchen.yaml") == {}
    assert get_device_metadata(tmp_path, "livingroom.yaml").get("comment") == "fresh"


async def test_metadata_store_rename_missing_old_entry_is_noop(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Store rename with no old entry leaves the target untouched (early-return guard)."""
    controller = make_controller(tmp_path)
    controller._metadata_store.update("livingroom.yaml", expected_config_hash="fresh")

    await controller._metadata_store.rename("kitchen.yaml", "livingroom.yaml")

    assert controller._metadata_store.get("kitchen.yaml") == {}
    assert controller._metadata_store.get("livingroom.yaml").get("expected_config_hash") == "fresh"


async def test_metadata_store_rename_same_name_is_noop(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Store rename with old == new leaves the entry untouched (early-return guard)."""
    controller = make_controller(tmp_path)
    controller._metadata_store.update("kitchen.yaml", expected_config_hash="keep")

    await controller._metadata_store.rename("kitchen.yaml", "kitchen.yaml")

    assert controller._metadata_store.get("kitchen.yaml").get("expected_config_hash") == "keep"


async def test_migrate_device_metadata_moves_both_stores(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Controller-level migration carries sidecar identity AND store volatile state."""
    controller = make_controller(tmp_path)
    await controller._shared_sidecar.update(
        "kitchen.yaml", labels=["a"], comment="downstairs", board_id="esp32dev"
    )
    controller._metadata_store.update("kitchen.yaml", expected_config_hash="deadbeef")

    await controller._migrate_device_metadata("kitchen.yaml", "livingroom.yaml")

    assert await controller._shared_sidecar.get("kitchen.yaml") == {}
    assert controller._metadata_store.get("kitchen.yaml") == {}
    sidecar = await controller._shared_sidecar.get("livingroom.yaml")
    assert sidecar.get("labels") == ["a"]
    assert sidecar.get("comment") == "downstairs"
    assert sidecar.get("board_id") == "esp32dev"
    store_entry = controller._metadata_store.get("livingroom.yaml")
    assert store_entry.get("expected_config_hash") == "deadbeef"


async def test_completed_rename_migrates_metadata_then_scans(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """The RENAME job-completion hook migrates labels before the rescan."""
    controller = make_controller(tmp_path)
    await controller._shared_sidecar.update("kitchen.yaml", labels=["a"], comment="downstairs")

    scheduled: list[Any] = []
    controller._db.create_background_task = scheduled.append

    job = FirmwareJob(
        job_id="abc123",
        configuration="kitchen.yaml",
        job_type=JobType.RENAME,
        status=JobStatus.COMPLETED,
        new_name="livingroom",
    )
    firmware_sync.on_job_completed(controller, Event(EventType.JOB_COMPLETED, {"job": job}))

    assert len(scheduled) == 1
    await scheduled[0]

    assert await controller._shared_sidecar.get("kitchen.yaml") == {}
    moved = await controller._shared_sidecar.get("livingroom.yaml")
    assert moved.get("labels") == ["a"]
    assert moved.get("comment") == "downstairs"
    assert controller._scanner.calls == [("scan",)]


async def test_completed_rename_normalizes_new_name_with_extension(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A ``new_name`` carrying ``.yaml`` still targets the bare-stem key."""
    controller = make_controller(tmp_path)
    await controller._shared_sidecar.update("kitchen.yaml", labels=["a"])

    scheduled: list[Any] = []
    controller._db.create_background_task = scheduled.append

    job = FirmwareJob(
        job_id="abc123",
        configuration="kitchen.yaml",
        job_type=JobType.RENAME,
        status=JobStatus.COMPLETED,
        new_name="livingroom.yaml",
    )
    firmware_sync.on_job_completed(controller, Event(EventType.JOB_COMPLETED, {"job": job}))

    await scheduled[0]

    moved = await controller._shared_sidecar.get("livingroom.yaml")
    assert moved.get("labels") == ["a"]
    assert await controller._shared_sidecar.get("livingroom.yaml.yaml") == {}


async def test_completed_rename_scans_even_when_migration_fails(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A failed metadata migration must not skip the rescan."""
    controller = make_controller(tmp_path)

    async def _boom(*_: Any) -> None:
        raise RuntimeError("migration failed")

    controller._migrate_device_metadata = _boom

    scheduled: list[Any] = []
    controller._db.create_background_task = scheduled.append

    job = FirmwareJob(
        job_id="abc123",
        configuration="kitchen.yaml",
        job_type=JobType.RENAME,
        status=JobStatus.COMPLETED,
        new_name="livingroom",
    )
    firmware_sync.on_job_completed(controller, Event(EventType.JOB_COMPLETED, {"job": job}))

    await scheduled[0]
    assert controller._scanner.calls == [("scan",)]


async def test_completed_rename_without_new_name_falls_back_to_scan(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A RENAME job missing ``new_name`` still rescans (no migration target)."""
    controller = make_controller(tmp_path)

    scheduled: list[Any] = []
    controller._db.create_background_task = scheduled.append

    job = FirmwareJob(
        job_id="abc123",
        configuration="kitchen.yaml",
        job_type=JobType.RENAME,
        status=JobStatus.COMPLETED,
    )
    firmware_sync.on_job_completed(controller, Event(EventType.JOB_COMPLETED, {"job": job}))

    assert len(scheduled) == 1
    await scheduled[0]
    assert controller._scanner.calls == [("scan",)]
