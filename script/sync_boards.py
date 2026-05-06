#!/usr/bin/env python3
"""
Generate ``definitions/boards.json`` from the per-board manifest YAMLs.

The YAML manifests under ``definitions/boards/<id>/manifest.yaml`` are
the human-editable source of truth; this script is the only thing
that writes ``boards.json``.

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

    # Abort the sync on the first bad manifest — partial output here
    # would silently ship a board-shaped hole to every install.
    catalog = build_board_catalog_from_manifests(strict=True)

    payload = catalog.to_dict()
    # ``OPT_SORT_KEYS`` keeps the output deterministic so manifest edits
    # produce minimal diffs in code review.
    _OUTPUT_FILE.write_bytes(
        orjson.dumps(payload, option=orjson.OPT_SORT_KEYS | orjson.OPT_APPEND_NEWLINE)
    )
    _LOGGER.info("Wrote %s (%d boards)", _OUTPUT_FILE, len(catalog.boards))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
