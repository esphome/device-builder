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
    Return the OTA address a rename flashes — the *pre-rename* device.

    ``StorageJSON.address`` wins (what the fused CLI resolved as
    ``CORE.address``, honouring ``wifi.use_address`` / ``manual_ip``
    from the last build); then the scanner's live hostname / IP; then
    the mDNS default for *fallback_name*.
    """
    storage = await run_in_executor(lambda: StorageJSON.load(resolve_storage_path(configuration)))
    if storage is not None and storage.address:
        return storage.address
    devices = controller._db.devices
    if devices is not None:
        device = devices.get_by_configuration(configuration)
        if device is not None:
            if device.address:
                return device.address
            if device.ip:
                return device.ip
    return default_mdns_address(fallback_name)
