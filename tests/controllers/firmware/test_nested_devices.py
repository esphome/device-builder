"""Firmware jobs for devices below ``devices/``: local builds, refusals, load errors."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import Device, ErrorCode, JobSource, JobType

from .conftest import (
    REMOTE_PIN,
    StubDevices,
    build_scheduler_inputs,
    stub_offloader,
    stub_pairing,
)

if TYPE_CHECKING:
    from .conftest import FirmwareControllerFactory

DESK = "devices/lights/desk.yaml"
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges")


def _write(tmp_path: Path, *configurations: str) -> None:
    for configuration in configurations:
        path = tmp_path / configuration
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("esphome:\n  name: x\n", encoding="utf-8")


def _remote_ready(controller: object) -> None:
    """Pair one idle build server so a compile is remote-eligible."""
    stub_offloader(
        controller,
        build_scheduler_inputs(
            pairings=[stub_pairing(pin_sha256=REMOTE_PIN)],
            open_pins={REMOTE_PIN},
            idle_pins={REMOTE_PIN},
        ),
    )


# Spellings of the nested device that still reach its file.
@pytest.mark.parametrize("configuration", [DESK, "./devices/lights/desk.yaml", "x/../" + DESK])
async def test_a_nested_device_compiles_locally_while_a_flat_one_goes_remote(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory, configuration: str
) -> None:
    controller = firmware_controller_factory(with_queue=True)
    _remote_ready(controller)
    _write(tmp_path, DESK, "kitchen.yaml")

    nested = await controller.compile(configuration=configuration)
    flat = await controller.compile(configuration="kitchen.yaml")

    assert nested.source is JobSource.LOCAL
    assert flat.source is JobSource.REMOTE_PENDING


async def test_a_nested_device_analyzes_memory_locally_while_a_flat_one_goes_remote(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    controller = firmware_controller_factory(with_queue=True)
    stub_offloader(
        controller,
        build_scheduler_inputs(
            pairings=[stub_pairing(pin_sha256=REMOTE_PIN, analyze_memory_supported=True)],
            open_pins={REMOTE_PIN},
            idle_pins={REMOTE_PIN},
        ),
    )
    _write(tmp_path, DESK, "kitchen.yaml")

    nested = await controller.analyze_memory(configuration="./" + DESK)
    flat = await controller.analyze_memory(configuration="kitchen.yaml")

    assert (nested.configuration, nested.source) == (DESK, JobSource.LOCAL)
    assert flat.source is JobSource.REMOTE_PENDING


async def test_cleaning_a_nested_device_queues_no_job_on_a_paired_build_server(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    controller = firmware_controller_factory(with_queue=True)
    _remote_ready(controller)
    _write(tmp_path, DESK, "kitchen.yaml")

    await controller.clean(configuration="./devices/lights/desk.yaml")
    await controller.clean(configuration="kitchen.yaml")

    sources = sorted(
        (job.configuration, job.source.value) for job in controller.state.jobs.values()
    )
    assert sources == [
        # The boundary hands the job the configuration the device list uses.
        (DESK, JobSource.LOCAL.value),
        ("kitchen.yaml", JobSource.LOCAL.value),
        ("kitchen.yaml", JobSource.REMOTE.value),
    ]


async def test_a_nested_device_installs_locally(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    controller = firmware_controller_factory(with_queue=True)
    _remote_ready(controller)
    controller._db.devices = StubDevices()  # type: ignore[attr-defined]
    _write(tmp_path, DESK, "kitchen.yaml")

    nested = await controller.install(configuration="./" + DESK, port="/dev/ttyUSB0")
    flat = await controller.install(configuration="kitchen.yaml", port="/dev/ttyUSB0")

    assert nested.source is JobSource.LOCAL
    assert flat.source is JobSource.REMOTE_PENDING


# Other spellings of the same file.
@pytest.mark.parametrize(
    "configuration",
    [
        DESK,
        "./" + DESK,
        "x/../" + DESK,
        "devices//lights/desk.yaml",
        "/" + DESK,
        DESK + "/",
        DESK + "/.",
        DESK + "//",
    ],
)
@pytest.mark.parametrize("op", ["compile", "install", "analyze_memory"])
async def test_a_device_sharing_its_file_name_queues_no_job(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
    op: str,
    configuration: str,
) -> None:
    controller = firmware_controller_factory(with_queue=True)
    error = "Another device file has the same file name (devices/b/desk.yaml)"
    device = Device(
        name="desk",
        friendly_name="desk",
        configuration=DESK,
        load_error=error,
        name_shared_with=("devices/b/desk.yaml",),
    )
    controller._db.devices = StubDevices([device])  # type: ignore[attr-defined]
    _write(tmp_path, DESK)
    if configuration.startswith("/"):
        configuration = f"{tmp_path.as_posix()}{configuration}"

    with pytest.raises(CommandError) as exc_info:
        await getattr(controller, op)(configuration=configuration)

    assert exc_info.value.code is ErrorCode.INVALID_ARGS
    assert str(exc_info.value) == error
    assert controller.state.jobs == {}


async def test_firmware_rename_of_a_nested_device_is_refused(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    controller = firmware_controller_factory(with_queue=True)
    _write(tmp_path, DESK)

    with pytest.raises(CommandError) as exc_info:
        await controller.rename(configuration=DESK, new_name="desk2")

    assert exc_info.value.code is ErrorCode.INVALID_ARGS
    assert "Renaming is not supported yet" in str(exc_info.value)
    assert not (tmp_path / "desk2.yaml").exists()
    assert controller.state.jobs == {}


async def test_a_missing_secrets_load_error_leaves_the_build_to_esphome(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """The missing-secrets error is a text scan (a comment can trip it), so it blocks nothing."""
    controller = firmware_controller_factory(with_queue=True)
    error = "Uses !secret, but devices/lights/secrets.yaml does not exist"
    device = Device(name="desk", friendly_name="desk", configuration=DESK, load_error=error)
    controller._db.devices = StubDevices([device])  # type: ignore[attr-defined]
    _write(tmp_path, DESK)

    job = await controller.compile(configuration=DESK)

    assert controller.state.jobs == {job.job_id: job}


@pytest.mark.parametrize(
    "configuration",
    [
        "./" + DESK,
        "x/../" + DESK,
        "DEVICES/lights/desk.yaml",
        "devices//lights/desk.yaml",
        "/" + DESK,
    ],
)
async def test_firmware_rename_of_a_nested_device_spelled_otherwise_is_refused(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory, configuration: str
) -> None:
    controller = firmware_controller_factory(with_queue=True)
    if configuration.startswith("/"):
        configuration = f"{tmp_path.as_posix()}{configuration}"
    _write(tmp_path, DESK, configuration)

    with pytest.raises(CommandError, match="Renaming is not supported yet"):
        await controller.rename(configuration=configuration, new_name="desk2")

    assert not (tmp_path / "desk2.yaml").exists()


async def test_firmware_rename_onto_a_nested_device_name_is_refused(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    controller = firmware_controller_factory(with_queue=True)
    _write(tmp_path, DESK, "kitchen.yaml")

    with pytest.raises(CommandError) as exc_info:
        await controller.rename(configuration="kitchen.yaml", new_name="desk")

    assert exc_info.value.code is ErrorCode.INVALID_ARGS
    assert str(exc_info.value) == (
        f"A device named desk.yaml already exists ({DESK}); device file names must be unique"
    )
    assert not (tmp_path / "desk.yaml").exists()
    assert controller.state.jobs == {}


def _shared_desk(controller: object) -> None:
    """Flag ``DESK`` as sharing its file name with ``devices/b/desk.yaml``."""
    device = Device(
        name="desk",
        friendly_name="desk",
        configuration=DESK,
        load_error="Another device file has the same file name (devices/b/desk.yaml)",
        name_shared_with=("devices/b/desk.yaml",),
    )
    controller._db.devices = StubDevices([device])  # type: ignore[attr-defined]


@posix_only
async def test_a_folder_symlink_onto_a_nested_device_builds_locally_and_is_guarded(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """``lights/desk.yaml`` through a top-level ``lights -> devices/lights`` is the nested one."""
    controller = firmware_controller_factory(with_queue=True)
    _remote_ready(controller)
    _write(tmp_path, DESK)
    (tmp_path / "lights").symlink_to("devices/lights", target_is_directory=True)

    job = await controller.compile(configuration="lights/desk.yaml")
    cleaned = await controller.clean(configuration="lights/desk.yaml")
    with pytest.raises(CommandError, match="Renaming is not supported yet"):
        await controller.rename(configuration="lights/desk.yaml", new_name="desk2")
    _shared_desk(controller)
    with pytest.raises(CommandError, match="same file name"):
        await controller.compile(configuration="lights/desk.yaml")

    assert (job.configuration, job.source) == (DESK, JobSource.LOCAL)
    # No REMOTE clean on the paired build server: the nested device never builds there.
    assert [j.source for j in controller.state.jobs.values() if j.job_type is JobType.CLEAN] == [
        JobSource.LOCAL
    ]
    assert cleaned.configuration == DESK
    assert not (tmp_path / "desk2.yaml").exists()


async def test_an_absolute_path_to_a_nested_device_builds_locally(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    controller = firmware_controller_factory(with_queue=True)
    _remote_ready(controller)
    _write(tmp_path, DESK)

    job = await controller.compile(configuration=str(tmp_path / DESK))

    assert (job.configuration, job.source) == (DESK, JobSource.LOCAL)


async def test_a_differently_cased_top_level_name_is_not_a_name_twin(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """Like the scanner's clash check, the guard is case-sensitive: ``Desk.yaml`` is its own."""
    controller = firmware_controller_factory(with_queue=True)
    _shared_desk(controller)
    _write(tmp_path, DESK, "Desk.yaml")

    job = await controller.compile(configuration="Desk.yaml")

    assert controller.state.jobs == {job.job_id: job}


async def test_firmware_rename_of_a_top_level_device_sharing_its_name_is_refused(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """The rename chain compiles, so it is held to the shared-name guard; config-only isn't."""
    controller = firmware_controller_factory(with_queue=True)
    error = "Another device file has the same file name (devices/b/desk.yaml)"
    device = Device(
        name="desk",
        friendly_name="desk",
        configuration="desk.yaml",
        load_error=error,
        name_shared_with=("devices/b/desk.yaml",),
    )
    controller._db.devices = StubDevices([device])  # type: ignore[attr-defined]
    _write(tmp_path, "desk.yaml", "devices/b/desk.yaml")

    with pytest.raises(CommandError) as exc_info:
        await controller.rename(configuration="desk.yaml", new_name="desk2")

    assert exc_info.value.code is ErrorCode.INVALID_ARGS
    assert str(exc_info.value) == error
    assert not (tmp_path / "desk2.yaml").exists()
    assert controller.state.jobs == {}


@pytest.mark.parametrize("new_name", ["devices/lights/pantry", "..\\pantry", "a/b"])
async def test_firmware_rename_onto_a_path_is_refused(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory, new_name: str
) -> None:
    controller = firmware_controller_factory(with_queue=True)
    _write(tmp_path, "kitchen.yaml")

    with pytest.raises(CommandError, match="new_name must be a device name, not a path"):
        await controller.rename(configuration="kitchen.yaml", new_name=new_name)

    assert controller.state.jobs == {}


async def test_a_peer_dashboard_job_is_not_held_to_this_dashboard_s_file_names(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """A build server's job for a peer's ``desk.yaml`` is that peer's device, not ours."""
    controller = firmware_controller_factory(with_queue=True)
    device = Device(
        name="desk",
        friendly_name="desk",
        configuration=DESK,
        load_error="Another device file has the same file name (desk.yaml)",
        name_shared_with=("desk.yaml",),
    )
    controller._db.devices = StubDevices([device])  # type: ignore[attr-defined]
    job = controller._create_job(
        ".esphome/.remote_builds/peer/desk/desk.yaml",
        JobType.COMPILE,
        remote_peer="peer",
        remote_job_id="remote-1",
    )

    await controller._enqueue(job)

    assert controller.state.jobs == {job.job_id: job}
