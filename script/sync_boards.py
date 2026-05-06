#!/usr/bin/env python3
"""
Generate definitions/boards.json from the per-board manifest YAMLs.

The board catalog ships as a checked-in JSON artefact so the dashboard
doesn't have to walk and parse ~500 ``manifest.yaml`` files at startup.
``yaml.safe_load`` is pure-Python and dominates startup on low-powered
hosts (>60 s on HA Green); ``orjson.loads`` of the same data is roughly
two orders of magnitude faster.

The YAML manifests under ``definitions/boards/<id>/manifest.yaml``
remain the human-editable source of truth — this script is the only
thing that should write ``boards.json``.

Usage
-----

    python script/sync_boards.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import orjson

# Make the package importable when running from a source checkout
# without ``pip install -e .``.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from esphome_device_builder.definitions import (  # noqa: E402
    build_board_catalog_from_manifests,
)

_LOGGER = logging.getLogger("sync_boards")

_OUTPUT_FILE = _REPO_ROOT / "esphome_device_builder" / "definitions" / "boards.json"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    # ``strict=True`` makes a single bad manifest abort the whole sync.
    # In production the runtime loader reads the prebuilt JSON which has
    # already passed CI, so loud failure here is the right trade-off.
    catalog = build_board_catalog_from_manifests(strict=True)

    payload = catalog.to_dict()
    # ``OPT_SORT_KEYS`` keeps the output deterministic so manifest edits
    # produce minimal diffs in code review. ``OPT_APPEND_NEWLINE``
    # mirrors ``script/sync_components.py`` for POSIX-friendly files.
    _OUTPUT_FILE.write_bytes(
        orjson.dumps(payload, option=orjson.OPT_SORT_KEYS | orjson.OPT_APPEND_NEWLINE)
    )
    _LOGGER.info("Wrote %s (%d boards)", _OUTPUT_FILE, len(catalog.boards))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
