"""Subprocess probe for ``test_download_path_does_not_import_esphome_components``.

Run as ``python <this> <tmpdir>``. Resolves downloads for an esp32 (answered from
the precomputed index) and a libretiny (answered by the device-builder-helper
child) device, then checks that neither ``esphome.components.esp32`` nor
``esphome.components.libretiny`` landed in this process's ``sys.modules``. Exits
non-zero, listing the leaked modules on stdout, if the invariant breaks.

Lives as its own module (not an embedded string) so it lints / type-checks like
the rest of the suite; it must run in a fresh interpreter for the sys.modules
check to be meaningful, hence the subprocess.
"""

from __future__ import annotations

import sys
from pathlib import Path

from esphome.storage_json import StorageJSON

from esphome_device_builder.controllers.firmware.download import collect_download_entries

_LEAK_PREFIXES = ("esphome.components.esp32", "esphome.components.libretiny")


def _storage(tmp: Path, target: str, *files: str) -> tuple[StorageJSON, Path]:
    build = tmp / target / "build"
    build.mkdir(parents=True)
    for name in files:
        (build / name).write_bytes(b"x")
    storage = StorageJSON(
        storage_version=1,
        name="demo",
        friendly_name=None,
        comment=None,
        esphome_version=None,
        src_version=None,
        address="demo.local",
        web_port=None,
        target_platform=target,
        build_path=str(build),
        firmware_bin_path=build / "firmware.bin",
        loaded_integrations=[],
        loaded_platforms=[],
        no_mdns=False,
    )
    path = tmp / f"{target}.json"
    storage.save(path)
    return storage, path


def main() -> int:
    tmp = Path(sys.argv[1])
    esp32_storage, esp32_path = _storage(tmp, "ESP32", "firmware.factory.bin")
    libretiny_storage, libretiny_path = _storage(tmp, "bk72xx", "firmware.uf2")
    collect_download_entries(esp32_storage, esp32_path)
    collect_download_entries(libretiny_storage, libretiny_path)

    leaked = sorted(
        name
        for name in sys.modules
        if name in _LEAK_PREFIXES or name.startswith(tuple(f"{p}." for p in _LEAK_PREFIXES))
    )
    if leaked:
        sys.stdout.write("\n".join(leaked))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
