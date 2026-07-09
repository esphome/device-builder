"""Single-vs-bulk parity for the firmware install/compile command pairs."""

from __future__ import annotations

import pytest

from esphome_device_builder.controllers.firmware.controller import FirmwareController
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import DeviceState, ErrorCode, JobType
from tests.conftest import make_device
from tests.controllers.firmware.conftest import FirmwareControllerFactory, StubDevices


def _job_shapes(controller: FirmwareController) -> list[tuple]:
    """Normalise queued jobs for cross-entry-point comparison."""
    return sorted(
        (
            job.job_type,
            job.configuration,
            job.port,
            bool(job.depends_on),
            job.source,
            job.is_deferred_install,
        )
        for job in controller.state.jobs.values()
    )


def _controller(factory: FirmwareControllerFactory, *devices) -> FirmwareController:
    controller = factory(with_queue=True)
    controller._db.devices = StubDevices(list(devices))
    return controller


async def test_install_parity_online_device_queues_the_same_chain(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    device = make_device("kitchen", state=DeviceState.ONLINE)
    single = _controller(firmware_controller_factory, device)
    bulk = _controller(firmware_controller_factory, device)

    await single.install(configuration="kitchen.yaml")
    await bulk.install_bulk(configurations=["kitchen.yaml"])

    assert _job_shapes(single) == _job_shapes(bulk)
    assert {job.job_type for job in single.state.jobs.values()} == {
        JobType.COMPILE,
        JobType.UPLOAD,
    }


async def test_install_parity_offline_device_defers_from_both_entry_points(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    device = make_device("kitchen", state=DeviceState.OFFLINE)
    single = _controller(firmware_controller_factory, device)
    bulk = _controller(firmware_controller_factory, device)

    await single.install(configuration="kitchen.yaml")
    await bulk.install_bulk(configurations=["kitchen.yaml"])

    assert _job_shapes(single) == _job_shapes(bulk)
    jobs = list(single.state.jobs.values())
    assert [job.job_type for job in jobs] == [JobType.COMPILE]
    assert jobs[0].is_deferred_install is True


async def test_install_parity_traversal_rejected_from_both_entry_points(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Both entry points reject with INVALID_ARGS; the batch check is atomic."""
    single = _controller(firmware_controller_factory)
    bulk = _controller(firmware_controller_factory)

    with pytest.raises(CommandError) as single_exc:
        await single.install(configuration="../evil.yaml")
    with pytest.raises(CommandError) as bulk_exc:
        await bulk.install_bulk(configurations=["kitchen.yaml", "../evil.yaml"])

    assert single_exc.value.code == bulk_exc.value.code == ErrorCode.INVALID_ARGS
    # The batched boundary validation fails the whole batch, so the
    # good config is not queued either — the documented single/bulk
    # difference for firmware verbs.
    assert not single.state.jobs
    assert not bulk.state.jobs


async def test_compile_parity_threads_force_local(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    device = make_device("kitchen", state=DeviceState.ONLINE)
    single = _controller(firmware_controller_factory, device)
    bulk = _controller(firmware_controller_factory, device)

    await single.compile(configuration="kitchen.yaml", force_local=True)
    await bulk.compile_bulk(configurations=["kitchen.yaml"], force_local=True)

    assert _job_shapes(single) == _job_shapes(bulk)
    assert [job.job_type for job in single.state.jobs.values()] == [JobType.COMPILE]
