"""
RP2040 build-tree files.

Two flash paths share this module:

* **BOOTSEL** (``upload_using_picotool``, esphome/__main__.py:992) —
  reads ``idedata.firmware_elf_path`` and uses ``cc_path`` to
  locate ``picotool`` in the same PIO packages dir. The
  materialiser remaps ``cc_path``'s PIO core prefix so picotool
  resolves on the offloader (see
  ``helpers.remote_artifacts_materialise._remap_pio_toolchain_path``).
* **Serial** (``upload_using_platformio``, esphome/__main__.py:961) —
  spawns ``pio run -t upload -t nobuild`` which reads ``firmware.uf2``
  / ``firmware.bin`` per the PIO recipe.

``firmware.elf`` is mandatory for BOOTSEL (picotool needs the
ELF, not just the UF2); ``firmware.uf2`` is mandatory for both
the BOOTSEL "drag onto mass-storage" path and the PIO upload
recipe; ``firmware.bin`` rides along for OTA.
"""

from __future__ import annotations

TARGET_PLATFORM = "rp2040"

BUILD_FILES: tuple[str, ...] = (
    ".pioenvs/{name}/firmware.bin",
    ".pioenvs/{name}/firmware.uf2",
    ".pioenvs/{name}/firmware.elf",
)
