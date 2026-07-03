"""Rename-chain helpers: resolve the pre-rename device's OTA address."""

from __future__ import annotations

from typing import TYPE_CHECKING

from esphome.storage_json import StorageJSON

from ...helpers.async_ import run_in_executor
from ...helpers.hostname import default_mdns_address
from ...helpers.storage_path import resolve_storage_path

if TYPE_CHECKING:
    from .controller import FirmwareController


async def resolve_old_device_address(
    controller: FirmwareController, configuration: str, fallback_name: str
) -> str:
    """
    Return the OTA address a rename flashes (the pre-rename device).

    Priority: ``StorageJSON.address`` (the fused CLI's ``CORE.address``,
    honours ``wifi.use_address``), then scanner hostname / IP, then the
    mDNS default for *fallback_name*.
    """
    storage = await run_in_executor(lambda: StorageJSON.load(resolve_storage_path(configuration)))
    # Annotated hop: upstream ``StorageJSON`` is untyped, so ``.address`` is Any.
    stored_address: str | None = storage.address if storage is not None else None
    if stored_address:
        return stored_address
    devices = controller._db.devices
    if devices is not None:
        device = devices.get_by_configuration(configuration)
        if device is not None:
            if device.address:
                return device.address
            if device.ip:
                return device.ip
    return default_mdns_address(fallback_name)
