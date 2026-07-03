"""Tests for the rename chain: routing, execution, revert, and restore."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.controllers.firmware import persistence, rename_flow
from esphome_device_builder.controllers.firmware.cli import build_command
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import (
    ErrorCode,
    FirmwareJob,
    JobSource,
    JobStatus,
    JobType,
)
from tests._storage_fixtures import write_storage_json
from tests.controllers.firmware.conftest import (
    build_scheduler_inputs,
    run_until_terminal,
    stub_offloader,
    stub_pairing,
    wire_background_tasks,
    wire_real_queue,
)

if TYPE_CHECKING:
    from esphome_device_builder.controllers.firmware import FirmwareController

    from .conftest import FirmwareControllerFactory

_PIN = "b" * 64

_KITCHEN_YAML = "esphome:\n  name: kitchen\n"


def _seed_kitchen(tmp_path: Path) -> None:
    (tmp_path / "kitchen.yaml").write_text(_KITCHEN_YAML, encoding="utf-8")


def _tail_of(controller: FirmwareController, head: FirmwareJob) -> FirmwareJob:
    return next(
        j
        for j in controller.state.jobs.values()
        if j.is_rename_tail and j.depends_on == head.job_id
    )


async def _wait_for_missing(path: Path, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while path.exists():
            await asyncio.sleep(0.01)


def _fake_esphome_recording(controller: FirmwareController, tmp_path: Path) -> Path:
    """Point ``esphome_cmd`` at a script that records argv and succeeds."""
    argv_log = tmp_path / "argv.jsonl"
    controller.state.esphome_cmd = [
        sys.executable,
        "-c",
        "import json, sys\n"
        f"with open({str(argv_log)!r}, 'a') as fh:\n"
        "    fh.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "print('INFO ok')\n"
        "sys.exit(0)\n",
    ]
    return argv_log


# ---------------------------------------------------------------------------
# Chain shape + scheduler routing (the #1812 regression)
# ---------------------------------------------------------------------------


async def test_rename_head_routes_remote_when_pairing_is_idle_and_connected(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """An eligible paired build server marks the rename's compile REMOTE_PENDING."""
    controller = firmware_controller_factory(with_queue=True)
    pairing = stub_pairing(pin_sha256=_PIN, label="desktop", esphome_version="2026.6.0")
    stub_offloader(
        controller,
        build_scheduler_inputs(pairings=[pairing], open_pins={_PIN}, idle_pins={_PIN}),
    )
    _seed_kitchen(tmp_path)

    head = await controller.rename(configuration="kitchen.yaml", new_name="livingroom")

    assert head.job_type is JobType.COMPILE
    assert head.source is JobSource.REMOTE_PENDING
    tail = _tail_of(controller, head)
    assert tail.source is JobSource.LOCAL


async def test_rename_head_routes_local_without_pairings(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    controller = firmware_controller_factory(with_queue=True)
    stub_offloader(
        controller, build_scheduler_inputs(pairings=[], open_pins=set(), idle_pins=set())
    )
    _seed_kitchen(tmp_path)

    head = await controller.rename(configuration="kitchen.yaml", new_name="livingroom")

    assert head.source is JobSource.LOCAL


async def test_rename_tail_carries_resolved_old_address(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """The tail's ``port`` is the old device's StorageJSON address at enqueue."""
    controller = firmware_controller_factory(with_queue=True)
    _seed_kitchen(tmp_path)
    write_storage_json(tmp_path, "kitchen.yaml", overrides={"address": "10.1.2.3"})

    head = await controller.rename(configuration="kitchen.yaml", new_name="livingroom")

    assert _tail_of(controller, head).port == "10.1.2.3"


async def test_rename_refuses_non_retargetable_name_without_side_effects(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    controller = firmware_controller_factory(with_queue=True)
    (tmp_path / "kitchen.yaml").write_text(
        "esphome:\n  name: kitchen_${suffix}\n", encoding="utf-8"
    )

    with pytest.raises(CommandError) as excinfo:
        await controller.rename(configuration="kitchen.yaml", new_name="livingroom")

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert not controller.state.jobs
    assert not (tmp_path / "livingroom.yaml").exists()


async def test_rename_retry_passes_target_exists_check(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """The prior chain's on-disk write doesn't read as a foreign collision."""
    controller = firmware_controller_factory(with_queue=True)
    wire_background_tasks(controller)
    _seed_kitchen(tmp_path)

    first = await controller.rename(configuration="kitchen.yaml", new_name="livingroom")
    assert (tmp_path / "livingroom.yaml").exists()

    retry = await controller.rename(configuration="kitchen.yaml", new_name="livingroom")

    assert first.status is JobStatus.CANCELLED
    assert retry.status is JobStatus.QUEUED
    # The retry's own write survives the superseded chain's revert.
    await asyncio.sleep(0.05)
    assert (tmp_path / "livingroom.yaml").exists()


async def test_rename_retry_to_new_target_reverts_the_old_target(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    controller = firmware_controller_factory(with_queue=True)
    wire_background_tasks(controller)
    _seed_kitchen(tmp_path)

    await controller.rename(configuration="kitchen.yaml", new_name="livingroom")
    await controller.rename(configuration="kitchen.yaml", new_name="den")

    await _wait_for_missing(tmp_path / "livingroom.yaml")
    assert (tmp_path / "den.yaml").exists()
    assert (tmp_path / "kitchen.yaml").exists()


# ---------------------------------------------------------------------------
# Execution end-to-end (real runner, fake esphome subprocess)
# ---------------------------------------------------------------------------


async def test_chain_success_flashes_old_address_and_swaps_files(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """Compile → released tail uploads the new YAML to the old address → swap."""
    controller = firmware_controller_factory(with_queue=True)
    wire_real_queue(controller)
    wire_background_tasks(controller)
    argv_log = _fake_esphome_recording(controller, tmp_path)
    _seed_kitchen(tmp_path)
    old_storage = write_storage_json(tmp_path, "kitchen.yaml", overrides={"address": "10.1.2.3"})
    (tmp_path / ".esphome" / "build" / "kitchen").mkdir(parents=True)

    head = await controller.rename(configuration="kitchen.yaml", new_name="livingroom")
    tail = _tail_of(controller, head)
    captured = await run_until_terminal(controller)

    assert head.status is JobStatus.COMPLETED
    assert tail.status is JobStatus.COMPLETED
    invocations = [json.loads(line) for line in argv_log.read_text().splitlines()]
    assert invocations[0][1] == "compile"
    assert invocations[0][2].endswith("livingroom.yaml")
    assert invocations[1][1] == "upload"
    assert invocations[1][2].endswith("livingroom.yaml")
    assert invocations[1][3:] == ["--device", "10.1.2.3"]
    # Swap: old YAML + storage + build tree gone, new YAML in place.
    assert not (tmp_path / "kitchen.yaml").exists()
    assert not old_storage.exists()
    assert not (tmp_path / ".esphome" / "build" / "kitchen").exists()
    assert (tmp_path / "livingroom.yaml").exists()
    assert len(captured["job_completed"]) == 2


async def test_compile_failure_cancels_tail_and_reverts(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    controller = firmware_controller_factory(with_queue=True)
    wire_real_queue(controller)
    wire_background_tasks(controller)
    controller.state.esphome_cmd = [sys.executable, "-c", "import sys; sys.exit(1)"]
    _seed_kitchen(tmp_path)

    head = await controller.rename(configuration="kitchen.yaml", new_name="livingroom")
    tail = _tail_of(controller, head)
    await run_until_terminal(controller)

    assert head.status is JobStatus.FAILED
    assert tail.status is JobStatus.CANCELLED
    await _wait_for_missing(tmp_path / "livingroom.yaml")
    assert (tmp_path / "kitchen.yaml").read_text(encoding="utf-8") == _KITCHEN_YAML


async def test_tail_failure_reverts_and_keeps_old_yaml(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """A failed flash deletes the new YAML; the old device files are untouched."""
    controller = firmware_controller_factory(with_queue=True)
    wire_real_queue(controller)
    wire_background_tasks(controller)
    # Succeed for the compile, fail for the upload.
    controller.state.esphome_cmd = [
        sys.executable,
        "-c",
        "import sys; sys.exit(0 if sys.argv[2] == 'compile' else 1)",
    ]
    _seed_kitchen(tmp_path)
    old_storage = write_storage_json(tmp_path, "kitchen.yaml")

    head = await controller.rename(configuration="kitchen.yaml", new_name="livingroom")
    tail = _tail_of(controller, head)
    await run_until_terminal(controller)

    assert head.status is JobStatus.COMPLETED
    assert tail.status is JobStatus.FAILED
    await _wait_for_missing(tmp_path / "livingroom.yaml")
    assert (tmp_path / "kitchen.yaml").exists()
    assert old_storage.exists()


async def test_cancelling_tail_cascades_to_its_compile(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """Cancelling the held tail also cancels the head — its build is doomed work."""
    controller = firmware_controller_factory(with_queue=True)
    wire_background_tasks(controller)
    _seed_kitchen(tmp_path)

    head = await controller.rename(configuration="kitchen.yaml", new_name="livingroom")
    tail = _tail_of(controller, head)
    await controller.cancel(job_id=tail.job_id)

    assert tail.status is JobStatus.CANCELLED
    assert head.status is JobStatus.CANCELLED
    await _wait_for_missing(tmp_path / "livingroom.yaml")


async def test_cancelling_head_cascades_to_tail_and_reverts(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    controller = firmware_controller_factory(with_queue=True)
    wire_background_tasks(controller)
    _seed_kitchen(tmp_path)

    head = await controller.rename(configuration="kitchen.yaml", new_name="livingroom")
    tail = _tail_of(controller, head)
    await controller.cancel(job_id=head.job_id)

    assert head.status is JobStatus.CANCELLED
    assert tail.status is JobStatus.CANCELLED
    await _wait_for_missing(tmp_path / "livingroom.yaml")
    assert (tmp_path / "kitchen.yaml").exists()


# ---------------------------------------------------------------------------
# Restore-after-restart routing
# ---------------------------------------------------------------------------


def _tail_job(status: JobStatus = JobStatus.QUEUED, depends_on: str = "head1") -> FirmwareJob:
    return FirmwareJob(
        job_id="tail1",
        configuration="kitchen.yaml",
        job_type=JobType.RENAME,
        status=status,
        new_name="livingroom",
        port="10.1.2.3",
        depends_on=depends_on,
    )


async def test_restored_tail_with_completed_prereq_lands_on_upload_lane(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    head = FirmwareJob(
        job_id="head1",
        configuration="livingroom.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.COMPLETED,
    )
    tail = _tail_job()
    controller = firmware_controller_factory(head, tail)

    persistence._restore_to_lane(controller, tail)

    assert controller.state.upload_lane.queue.get_nowait() is tail


async def test_restored_tail_with_missing_prereq_cancels_and_reverts(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """A pruned/failed prerequisite cancels the restored tail and cleans its write."""
    tail = _tail_job()
    controller = firmware_controller_factory(tail)
    wire_background_tasks(controller)
    (tmp_path / "livingroom.yaml").write_text("esphome:\n  name: livingroom\n", encoding="utf-8")

    persistence._restore_to_lane(controller, tail)

    assert tail.status is JobStatus.CANCELLED
    await _wait_for_missing(tmp_path / "livingroom.yaml")


async def test_restored_legacy_rename_lands_on_compile_lane(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A persisted pre-decomposition RENAME keeps the fused compile-lane path."""
    legacy = _tail_job(depends_on="")
    controller = firmware_controller_factory(legacy)
    controller.state.compile_lane.queue = asyncio.Queue()

    persistence._restore_to_lane(controller, legacy)

    assert controller.state.compile_lane.queue.get_nowait() is legacy


def test_legacy_rename_command_shape_is_unchanged() -> None:
    cmd = build_command(["esphome"], JobType.RENAME, "kitchen.yaml", "", None, "livingroom")
    assert cmd == ["esphome", "--dashboard", "rename", "kitchen.yaml", "livingroom"]


# ---------------------------------------------------------------------------
# Revert / finalize edge branches
# ---------------------------------------------------------------------------


async def test_rename_rejected_by_foreign_chain_rolls_back_and_writes_nothing(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """Another device's active rename to the same target rejects the whole chain."""
    foreign = FirmwareJob(
        job_id="rn0",
        configuration="garage.yaml",
        job_type=JobType.RENAME,
        status=JobStatus.QUEUED,
        new_name="livingroom",
        depends_on="c0",
    )
    controller = firmware_controller_factory(foreign, with_queue=True)
    _seed_kitchen(tmp_path)

    in_flight = FirmwareJob(
        job_id="c9",
        configuration="kitchen.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.QUEUED,
    )
    controller.state.jobs[in_flight.job_id] = in_flight

    with pytest.raises(CommandError) as excinfo:
        await controller.rename(configuration="kitchen.yaml", new_name="livingroom")

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert sorted(controller.state.jobs) == ["c9", "rn0"]
    assert not (tmp_path / "livingroom.yaml").exists()
    # A rejected rename must not supersede the device's in-flight build.
    assert in_flight.status is JobStatus.QUEUED


async def test_revert_skips_when_a_newer_chain_owns_the_target(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    newer = _tail_job()
    controller = firmware_controller_factory(newer)
    (tmp_path / "livingroom.yaml").write_text("esphome:\n  name: livingroom\n", encoding="utf-8")
    superseded = FirmwareJob(
        job_id="tail0",
        configuration="kitchen.yaml",
        job_type=JobType.RENAME,
        status=JobStatus.CANCELLED,
        new_name="livingroom",
        depends_on="head0",
    )

    await rename_flow.revert_rename(controller, superseded)

    assert (tmp_path / "livingroom.yaml").exists()


async def test_revert_reloads_the_scanner_when_devices_is_up(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    controller = firmware_controller_factory()
    devices = MagicMock()
    devices.reload_configuration = AsyncMock()
    controller._db.devices = devices
    (tmp_path / "livingroom.yaml").write_text("", encoding="utf-8")
    new_storage = write_storage_json(tmp_path, "livingroom.yaml")
    (tmp_path / ".esphome" / "build" / "livingroom").mkdir(parents=True)
    tail = _tail_job(status=JobStatus.FAILED)

    await rename_flow.revert_rename(controller, tail)

    # The head compile's outputs for the new name are cleaned with the YAML.
    assert not (tmp_path / "livingroom.yaml").exists()
    assert not new_storage.exists()
    assert not (tmp_path / ".esphome" / "build" / "livingroom").exists()
    devices.reload_configuration.assert_awaited_once_with("livingroom.yaml")


def test_on_job_terminal_ignores_completed_tails_and_non_tails(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    controller = firmware_controller_factory()
    controller._db.create_background_task = MagicMock()

    rename_flow.on_job_terminal(controller, _tail_job(status=JobStatus.COMPLETED))
    upload = FirmwareJob(
        job_id="u1",
        configuration="kitchen.yaml",
        job_type=JobType.UPLOAD,
        status=JobStatus.FAILED,
        depends_on="c1",
    )
    rename_flow.on_job_terminal(controller, upload)

    controller._db.create_background_task.assert_not_called()


async def test_rename_retargets_a_local_substitution_definition(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """A ``${var}`` name rewrites the substitution def; the leaf keeps the indirection."""
    controller = firmware_controller_factory(with_queue=True)
    (tmp_path / "kitchen.yaml").write_text(
        "substitutions:\n  devicename: kitchen\nesphome:\n  name: ${devicename}\n",
        encoding="utf-8",
    )

    head = await controller.rename(configuration="kitchen.yaml", new_name="livingroom")

    new_content = (tmp_path / "livingroom.yaml").read_text(encoding="utf-8")
    assert "  devicename: livingroom\n" in new_content
    assert "  name: ${devicename}\n" in new_content
    # The flash fallback resolves the *old* name through the substitution.
    assert _tail_of(controller, head).port == "kitchen.local"


async def test_ws_direct_rename_of_missing_yaml_raises_not_found(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """The ``firmware/rename`` entry point reads the YAML itself and errors typed."""
    controller = firmware_controller_factory(with_queue=True)

    with pytest.raises(CommandError) as excinfo:
        await controller.rename(configuration="kitchen.yaml", new_name="livingroom")

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "not found" in excinfo.value.message


async def test_rename_chain_uses_caller_supplied_content(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """``devices/rename`` threads its read + rewrite through; nothing is re-derived."""
    controller = firmware_controller_factory(with_queue=True)
    _seed_kitchen(tmp_path)

    await controller.rename_chain(
        configuration="kitchen.yaml",
        new_name="livingroom",
        content=_KITCHEN_YAML,
        new_content="esphome:\n  name: livingroom\n  comment: threaded\n",
    )

    written = (tmp_path / "livingroom.yaml").read_text(encoding="utf-8")
    assert "comment: threaded" in written


@pytest.mark.parametrize("boom", [OSError("read-only fs"), RuntimeError("wat")])
async def test_finalize_swap_failure_logs_and_never_raises(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
    boom: Exception,
) -> None:
    """The device already runs the renamed firmware; a swap error must not fail the job."""
    controller = firmware_controller_factory()

    def _boom(_path: object, _configuration: str) -> None:
        raise boom

    monkeypatch.setattr(
        "esphome_device_builder.controllers.firmware.rename_flow.remove_device_files", _boom
    )
    _seed_kitchen(tmp_path)

    await rename_flow.finalize_rename_swap(controller, _tail_job(status=JobStatus.RUNNING))

    assert (tmp_path / "kitchen.yaml").exists()


async def test_revert_skips_its_own_active_entry(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """The reverted tail's own jobs entry doesn't read as a newer owning chain."""
    tail = _tail_job()
    controller = firmware_controller_factory(tail)
    (tmp_path / "livingroom.yaml").write_text("", encoding="utf-8")

    await rename_flow.revert_rename(controller, tail)

    assert not (tmp_path / "livingroom.yaml").exists()


async def test_revert_cleanup_failure_logs_and_skips_the_reload(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = firmware_controller_factory()
    devices = MagicMock()
    devices.reload_configuration = AsyncMock()
    controller._db.devices = devices

    def _boom(_path: object, _configuration: str) -> None:
        raise OSError("read-only fs")

    monkeypatch.setattr(
        "esphome_device_builder.controllers.firmware.rename_flow.remove_device_files", _boom
    )

    await rename_flow.revert_rename(controller, _tail_job(status=JobStatus.FAILED))

    devices.reload_configuration.assert_not_awaited()


async def test_chain_persists_before_the_yaml_write(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash between persist and write must restore a chain, not strand a file."""
    controller = firmware_controller_factory(with_queue=True)
    _seed_kitchen(tmp_path)
    order: list[str] = []
    controller._persist_jobs.side_effect = lambda: order.append("persist")
    monkeypatch.setattr(
        "esphome_device_builder.controllers.firmware.rename_flow.atomic_write_file",
        lambda _path, _content: order.append("write"),
    )

    await controller.rename(configuration="kitchen.yaml", new_name="livingroom")

    assert order.index("persist") < order.index("write")
