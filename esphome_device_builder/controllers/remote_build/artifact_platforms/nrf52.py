"""
nRF52 (Zephyr) build-tree files.

The nRF52 platform component ships its own ``upload_program``
(esphome/components/nrf52/__init__.py:407) which reads
``CORE.relative_pioenvs_path(CORE.name, "zephyr", "app_update.bin")``
for BLE OTA via smpclient. We ship that file plus ``zephyr.elf``
for symbol resolution; both live under ``.pioenvs/<name>/zephyr/``.

``CORE.firmware_bin`` falls through to the default branch
(esphome/core/__init__.py:780) returning
``.pioenvs/<name>/firmware.bin`` — Zephyr also emits that file
as a side-effect of the smpclient build, so we ship it too for
any fallback path that resolves through StorageJSON.firmware_bin_path
(the dashboard's firmware/download endpoint, build_size helpers).
"""

from __future__ import annotations

TARGET_PLATFORM = "nrf52"

BUILD_FILES: tuple[str, ...] = (
    ".pioenvs/{name}/firmware.bin",
    ".pioenvs/{name}/firmware.elf",
    ".pioenvs/{name}/zephyr/app_update.bin",
    ".pioenvs/{name}/zephyr/zephyr.elf",
)
