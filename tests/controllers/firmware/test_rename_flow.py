"""Tests for ``rename_flow.resolve_old_device_address``'s fallback order."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from esphome_device_builder.controllers.firmware.rename_flow import (
    resolve_old_device_address,
)
from tests._storage_fixtures import write_storage_json
from tests.controllers.firmware.conftest import BareFirmwareControllerFactory


def _wire_devices(controller: MagicMock, *, address: str = "", ip: str = "") -> None:
    device = MagicMock()
    device.address = address
    device.ip = ip
    devices = MagicMock()
    devices.get_by_configuration.return_value = device
    controller._db.devices = devices


@pytest.mark.asyncio
async def test_storage_address_wins(
    tmp_path: Path,
    bare_firmware_controller_factory: BareFirmwareControllerFactory,
) -> None:
    controller = bare_firmware_controller_factory(with_mock_db=True)
    _wire_devices(controller, address="scanner.local", ip="10.0.0.9")
    write_storage_json(tmp_path, "kitchen.yaml", overrides={"address": "use-addr.example"})

    assert await resolve_old_device_address(controller, "kitchen.yaml", "kitchen") == (
        "use-addr.example"
    )


@pytest.mark.asyncio
async def test_scanner_address_when_storage_missing(
    bare_firmware_controller_factory: BareFirmwareControllerFactory,
) -> None:
    controller = bare_firmware_controller_factory(with_mock_db=True)
    _wire_devices(controller, address="scanner.local", ip="10.0.0.9")

    assert await resolve_old_device_address(controller, "kitchen.yaml", "kitchen") == (
        "scanner.local"
    )


@pytest.mark.asyncio
async def test_scanner_ip_when_storage_address_empty(
    tmp_path: Path,
    bare_firmware_controller_factory: BareFirmwareControllerFactory,
) -> None:
    controller = bare_firmware_controller_factory(with_mock_db=True)
    _wire_devices(controller, ip="10.0.0.9")
    write_storage_json(tmp_path, "kitchen.yaml", overrides={"address": ""})

    assert await resolve_old_device_address(controller, "kitchen.yaml", "kitchen") == "10.0.0.9"


@pytest.mark.asyncio
async def test_mdns_default_when_nothing_known(
    bare_firmware_controller_factory: BareFirmwareControllerFactory,
) -> None:
    controller = bare_firmware_controller_factory(with_mock_db=True)

    assert await resolve_old_device_address(controller, "kitchen.yaml", "kitchen_1") == (
        "kitchen_1.local"
    )


@pytest.mark.asyncio
async def test_mdns_default_when_device_unknown_to_scanner(
    bare_firmware_controller_factory: BareFirmwareControllerFactory,
) -> None:
    controller = bare_firmware_controller_factory(with_mock_db=True)
    devices = MagicMock()
    devices.get_by_configuration.return_value = None
    controller._db.devices = devices

    assert await resolve_old_device_address(controller, "kitchen.yaml", "kitchen") == (
        "kitchen.local"
    )
