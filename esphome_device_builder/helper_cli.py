"""Internal subprocess helpers that need ``esphome.components`` imported.

Run as ``device-builder-helper <command>`` (the package's ``[project.scripts]``
entry point) or ``python -m esphome_device_builder.helper_cli``. The dashboard
spawns this so its long-lived process never imports heavy ``esphome.components``
modules (esp32 pulls espidf -> requests -> esphome.config); the child does the
import, prints JSON to stdout, and exits.

Commands:
  download-types <storage-json-path> <component>
      Print ``[{title, description, file}]`` from
      ``esphome.components.<component>.get_download_types`` for the device whose
      StorageJSON sidecar is at the given path. Used for the build-dir-dependent
      platforms (libretiny / nrf52) the generated catalog can't precompute.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

from esphome.storage_json import StorageJSON


def _cmd_download_types(args: argparse.Namespace) -> int:
    storage = StorageJSON.load(Path(args.storage_path))
    if storage is None:
        json.dump([], sys.stdout)
        return 0
    module = importlib.import_module(f"esphome.components.{args.component}")
    entries = [
        {
            "title": entry.get("title", ""),
            "description": entry.get("description", ""),
            "file": entry["file"],
        }
        for entry in module.get_download_types(storage)
    ]
    json.dump(entries, sys.stdout)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="device-builder-helper", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    download_types = sub.add_parser(
        "download-types", help="Print get_download_types JSON for a device's storage."
    )
    download_types.add_argument("storage_path", help="Path to the StorageJSON sidecar.")
    download_types.add_argument("component", help="esphome.components.<component> to query.")
    download_types.set_defaults(func=_cmd_download_types)
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
