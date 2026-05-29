#!/usr/bin/env python3
"""
Generate the split board catalog from the per-board manifest YAMLs.

Emits three artefacts under ``esphome_device_builder/definitions/``:

* ``boards.index.json`` — slim ``BoardCatalogIndex`` per board (picker
  fields only: identity, esphome platform/board/variant, tags, images,
  urls, sort flags). This is what ``boards/get_boards`` ships.
* ``board_bodies/<id>.json`` — full body per board (hardware, pins,
  featured_components, featured_bundles, default_components). Lazy-
  loaded via :class:`LazyBodyStore` on ``boards/get_board``. The
  directory name is distinct from the manifests dir at
  ``definitions/boards/<id>/manifest.yaml`` so the body-swap rmtree
  can't trample the hand-curated source.
* ``featured_components.index.json`` — aggregated
  ``{board_id: list[FeaturedComponent]}`` for the components
  controller's startup registry build. Lets ``components.py``
  hook up cross-catalog references without ever touching board
  bodies.

The YAML manifests under ``definitions/boards/<id>/manifest.yaml``
remain the human-editable source of truth; this script is the only
thing that writes the three artefacts.

Usage
-----

    python script/sync_boards.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import orjson

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _catalog_split import (  # noqa: E402
    emit_body_with_roundtrip,
    prepare_next_bodies_dir,
    swap_split_catalog_in,
)

from esphome_device_builder.definitions import (  # noqa: E402
    build_board_catalog_from_manifests,
)
from esphome_device_builder.models import BoardCatalogEntry, BoardCatalogIndex  # noqa: E402

_LOGGER = logging.getLogger("sync_boards")

_DEFINITIONS_DIR = _REPO_ROOT / "esphome_device_builder" / "definitions"
_INDEX_FILE = _DEFINITIONS_DIR / "boards.index.json"
_BODIES_DIR = _DEFINITIONS_DIR / "board_bodies"
_FEATURED_INDEX_FILE = _DEFINITIONS_DIR / "featured_components.index.json"

# Fields stripped from the slim index entry — they belong on the
# per-board body file only.
_INDEX_DROP_FIELDS: frozenset[str] = frozenset(
    {"hardware", "pins", "featured_components", "featured_bundles", "default_components"}
)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    # Abort the sync on the first bad manifest — partial output here
    # would silently ship a board-shaped hole to every install.
    catalog = build_board_catalog_from_manifests(strict=True)

    # ``to_dict`` here already applies the omit_default Configs, so
    # body files and index entries both ship the stripped wire shape.
    full_payloads = [board.to_dict() for board in catalog.boards]
    _emit_split_catalog(catalog.boards, full_payloads)
    _emit_featured_components_index(catalog.boards)

    _LOGGER.info(
        "Wrote %s + %d body files under %s + %s",
        _INDEX_FILE,
        len(catalog.boards),
        _BODIES_DIR,
        _FEATURED_INDEX_FILE,
    )
    return 0


def _emit_split_catalog(
    boards: list[BoardCatalogEntry], full_payloads: list[dict[str, Any]]
) -> None:
    """Write ``boards.index.json`` + ``board_bodies/<id>.json`` via atomic swap."""
    next_bodies = _BODIES_DIR.parent / "board_bodies.next"
    prepare_next_bodies_dir(next_bodies)

    for board, payload in zip(boards, full_payloads, strict=True):
        # Body files carry the full BoardCatalogEntry payload — they
        # round-trip through ``BoardCatalogEntry.from_dict`` standalone,
        # mirroring the automations / components split where each
        # body file is self-describing.
        emit_body_with_roundtrip(
            payload,
            board.id,
            next_bodies,
            BoardCatalogEntry,
            log_label="Board",
            sort_keys=True,
        )

    index_payload = {
        "boards": sorted(
            (_strip_body_fields(payload) for payload in full_payloads),
            key=lambda p: p["id"],
        ),
    }
    swap_split_catalog_in(
        next_bodies=next_bodies,
        live_bodies=_BODIES_DIR,
        index_payload=index_payload,
        live_index=_INDEX_FILE,
        index_cls=BoardCatalogIndex,
        index_entries_key="boards",
        sort_keys=True,
    )


def _emit_featured_components_index(boards: list[BoardCatalogEntry]) -> None:
    """Write the aggregated ``{board_id: list[FeaturedComponent]}`` index.

    The components controller hits this once at startup to build its
    cross-catalog featured-component registry without ever touching
    per-board body files. Boards with no featured components are
    omitted so the file stays tight.
    """
    payload: dict[str, list[dict[str, Any]]] = {}
    for board in boards:
        if not board.featured_components:
            continue
        payload[board.id] = [fc.to_dict() for fc in board.featured_components]
    next_path = _FEATURED_INDEX_FILE.with_suffix(".json.next")
    next_path.write_bytes(
        orjson.dumps(payload, option=orjson.OPT_SORT_KEYS | orjson.OPT_APPEND_NEWLINE)
    )
    next_path.replace(_FEATURED_INDEX_FILE)


def _strip_body_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Return *payload* with body-only keys removed (slim shape)."""
    return {k: v for k, v in payload.items() if k not in _INDEX_DROP_FIELDS}


if __name__ == "__main__":
    raise SystemExit(main())
