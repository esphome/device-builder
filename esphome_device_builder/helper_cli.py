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
import contextlib
import importlib
import re
import sys
from pathlib import Path

from esphome.storage_json import StorageJSON

from .definitions import coerce_download_entries
from .helpers.json import dumps_str

# esphome component module names are lowercase identifiers. Validate before
# interpolating into the import path so a crafted target_platform can't steer
# ``import_module`` to a dotted sub-path or an unexpected module (defence in
# depth: the name is already confined to the ``esphome.components.`` prefix and
# import_module is not eval, but keep the surface minimal).
_COMPONENT_RE = re.compile(r"[a-z0-9_]+")


def _cmd_download_types(args: argparse.Namespace) -> int:
    storage = StorageJSON.load(Path(args.storage_path))
    if storage is None or not _COMPONENT_RE.fullmatch(args.component):
        sys.stdout.write(dumps_str([]))
        return 0
    # Keep stdout pure JSON: route anything esphome prints to stdout during the
    # component import / get_download_types to stderr, so the parent's parse
    # can't choke on a banner or deprecation notice. ``coerce_download_entries``
    # shapes + tolerates a malformed entry (no string ``file``) instead of one
    # bad entry aborting the whole reply.
    with contextlib.redirect_stdout(sys.stderr):
        module = importlib.import_module(f"esphome.components.{args.component}")
        entries = coerce_download_entries(module.get_download_types(storage))
    sys.stdout.write(dumps_str(entries))
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


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
