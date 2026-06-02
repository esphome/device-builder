"""Tests for stale-first bulk firmware ordering."""

from __future__ import annotations

from esphome_device_builder.controllers.firmware.bulk import _esphome_version_sort_key
from esphome_device_builder.models import Device, JobType
from tests.conftest import make_device
from tests.controllers.firmware.conftest import FirmwareControllerFactory


class _DevicesController:
    def __init__(self, devices: list[Device]) -> None:
        self._devices = devices

    def get_devices(self) -> list[Device]:
        return self._devices


def _device(
    configuration: str,
    *,
    current_version: str = "2026.5.0",
    deployed_version: str = "2026.5.0",
    has_pending_changes: bool = False,
    update_available: bool = False,
) -> Device:
    name = configuration.removesuffix(".yaml")
    return make_device(
        name=name,
        friendly_name=name,
        configuration=configuration,
        address="",
        current_version=current_version,
        deployed_version=deployed_version,
        has_pending_changes=has_pending_changes,
        update_available=update_available,
    )


async def test_install_bulk_queues_stale_devices_before_pending_changes(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    controller = firmware_controller_factory(with_queue=True)
    controller._db.devices = _DevicesController(
        [
            _device("current.yaml"),
            _device("pending.yaml", has_pending_changes=True),
            _device(
                "newer-old.yaml",
                deployed_version="2025.12.0",
                update_available=True,
            ),
            _device(
                "oldest.yaml",
                deployed_version="2024.1.0",
                update_available=True,
            ),
        ]
    )

    jobs = await controller.install_bulk(
        configurations=[
            "current.yaml",
            "pending.yaml",
            "newer-old.yaml",
            "oldest.yaml",
            "unknown.yaml",
        ]
    )

    assert [job.configuration for job in jobs] == [
        "oldest.yaml",
        "newer-old.yaml",
        "pending.yaml",
        "current.yaml",
        "unknown.yaml",
    ]
    # install_bulk now queues a chain per device; the returned jobs are the
    # COMPILE heads (each with a dependent UPLOAD held behind it).
    assert [job.job_type for job in jobs] == [JobType.COMPILE] * 5


async def test_bulk_order_preserves_tail_input_order(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    controller = firmware_controller_factory(with_queue=True)
    controller._db.devices = _DevicesController(
        [
            _device("current-a.yaml", deployed_version="2026.5.0"),
            _device("current-b.yaml", deployed_version="2026.5.0"),
            _device("pending.yaml", has_pending_changes=True),
            _device("stale.yaml", deployed_version="2024.1.0", update_available=True),
        ]
    )

    jobs = await controller.install_bulk(
        configurations=[
            "current-a.yaml",
            "unknown.yaml",
            "current-b.yaml",
            "pending.yaml",
            "stale.yaml",
        ]
    )

    assert [job.configuration for job in jobs] == [
        "stale.yaml",
        "pending.yaml",
        "current-a.yaml",
        "unknown.yaml",
        "current-b.yaml",
    ]


async def test_bulk_order_keeps_newer_deployed_versions_in_tail(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    controller = firmware_controller_factory(with_queue=True)
    controller._db.devices = _DevicesController(
        [
            _device(
                "newer.yaml",
                current_version="2026.5.0",
                deployed_version="2026.6.0",
                update_available=True,
            ),
            _device("pending.yaml", has_pending_changes=True),
            _device("stale.yaml", deployed_version="2024.1.0", update_available=True),
            _device("current.yaml"),
        ]
    )

    jobs = await controller.install_bulk(
        configurations=["newer.yaml", "pending.yaml", "stale.yaml", "current.yaml"]
    )

    assert [job.configuration for job in jobs] == [
        "stale.yaml",
        "pending.yaml",
        "newer.yaml",
        "current.yaml",
    ]


async def test_bulk_order_uses_update_available_as_stale_gate(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    controller = firmware_controller_factory(with_queue=True)
    controller._db.devices = _DevicesController(
        [
            _device(
                "behind-but-not-flagged.yaml",
                current_version="2026.5.0",
                deployed_version="2024.1.0",
                update_available=False,
            ),
            _device("pending.yaml", has_pending_changes=True),
            _device("stale.yaml", deployed_version="2025.1.0", update_available=True),
        ]
    )

    jobs = await controller.install_bulk(
        configurations=["behind-but-not-flagged.yaml", "pending.yaml", "stale.yaml"]
    )

    assert [job.configuration for job in jobs] == [
        "stale.yaml",
        "pending.yaml",
        "behind-but-not-flagged.yaml",
    ]


async def test_bulk_order_sorts_prerelease_numbers_numerically(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    controller = firmware_controller_factory(with_queue=True)
    controller._db.devices = _DevicesController(
        [
            _device(
                "beta-10.yaml",
                current_version="2026.6.0",
                deployed_version="2026.6.0b10",
                update_available=True,
            ),
            _device(
                "beta-2.yaml",
                current_version="2026.6.0",
                deployed_version="2026.6.0b2",
                update_available=True,
            ),
        ]
    )

    jobs = await controller.install_bulk(configurations=["beta-10.yaml", "beta-2.yaml"])

    assert [job.configuration for job in jobs] == ["beta-2.yaml", "beta-10.yaml"]


async def test_bulk_order_keeps_missing_versions_out_of_stale_bucket(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    controller = firmware_controller_factory(with_queue=True)
    controller._db.devices = _DevicesController(
        [
            _device("missing-deployed.yaml", deployed_version="", update_available=True),
            _device(
                "missing-current.yaml",
                current_version="",
                deployed_version="2025.1.0",
                update_available=True,
            ),
            _device("pending.yaml", has_pending_changes=True),
            _device("stale.yaml", deployed_version="2025.1.0", update_available=True),
        ]
    )

    jobs = await controller.install_bulk(
        configurations=[
            "missing-deployed.yaml",
            "missing-current.yaml",
            "pending.yaml",
            "stale.yaml",
        ]
    )

    assert [job.configuration for job in jobs] == [
        "stale.yaml",
        "pending.yaml",
        "missing-deployed.yaml",
        "missing-current.yaml",
    ]


async def test_bulk_order_treats_unknown_deployed_version_as_oldest_stale(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    controller = firmware_controller_factory(with_queue=True)
    controller._db.devices = _DevicesController(
        [
            _device(
                "local-build.yaml",
                deployed_version="local-build",
                update_available=True,
            ),
            _device("old.yaml", deployed_version="2025.1.0", update_available=True),
        ]
    )

    jobs = await controller.install_bulk(configurations=["old.yaml", "local-build.yaml"])

    assert [job.configuration for job in jobs] == ["local-build.yaml", "old.yaml"]


def test_esphome_version_sort_key_handles_missing_versions() -> None:
    assert _esphome_version_sort_key("") == _esphome_version_sort_key(None)


async def test_compile_bulk_uses_stale_first_order(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    controller = firmware_controller_factory(with_queue=True)
    controller._db.devices = _DevicesController(
        [
            _device("fresh.yaml"),
            _device("old.yaml", deployed_version="2025.1.0", update_available=True),
        ]
    )

    jobs = await controller.compile_bulk(configurations=["fresh.yaml", "old.yaml"])

    assert [job.configuration for job in jobs] == ["old.yaml", "fresh.yaml"]
    assert [job.job_type for job in jobs] == [JobType.COMPILE, JobType.COMPILE]


async def test_bulk_order_preserves_input_without_devices_controller(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    controller = firmware_controller_factory(with_queue=True)
    controller._db.devices = None

    jobs = await controller.install_bulk(configurations=["first.yaml", "second.yaml", "third.yaml"])

    assert [job.configuration for job in jobs] == [
        "first.yaml",
        "second.yaml",
        "third.yaml",
    ]
