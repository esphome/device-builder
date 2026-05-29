"""Shared atomic-swap helpers for the split-catalog sync scripts.

Used by ``script/sync_components.py`` (components + automations
catalogs) and ``script/sync_boards.py`` (boards catalog). Each
emits a slim ``<catalog>.index.json`` plus a sibling tree of
per-id ``<id>.json`` body files; the helpers below cover the
common "stage in a tempdir, then atomic-swap into place" shape.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any, Protocol

import orjson

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from esphome_device_builder.helpers.lazy_catalog import is_unsafe_catalog_id  # noqa: E402

__all__ = [
    "emit_body_with_roundtrip",
    "is_unsafe_catalog_id",
    "prepare_next_bodies_dir",
    "swap_split_catalog_in",
]


class _FromDict(Protocol):
    """Protocol for mashumaro dataclasses with a ``from_dict`` classmethod."""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Any: ...


def prepare_next_bodies_dir(next_bodies: Path) -> None:
    """Wipe and recreate the sibling tempdir bodies land in before the swap."""
    if next_bodies.exists():
        shutil.rmtree(next_bodies)
    next_bodies.mkdir(parents=True)


def emit_body_with_roundtrip(
    body: dict[str, Any],
    cid: str,
    body_dir: Path,
    body_cls: type[_FromDict],
    *,
    log_label: str,
    sort_keys: bool = False,
) -> None:
    """Write one body file after traversal + mashumaro roundtrip validation.

    Mirrors the runtime body loader's path-traversal guard on the
    write side; a sync-time bug or upstream schema change introducing
    a separator / parent ref in an id would silently escape
    ``body_dir`` without this check. Roundtrip-validates the body
    through ``body_cls.from_dict`` so a shape drift fails the build
    rather than landing as a half-populated catalog at runtime.
    """
    if is_unsafe_catalog_id(cid):
        msg = f"Refusing to emit {log_label} body for traversal-shaped id: {cid!r}"
        raise ValueError(msg)
    try:
        body_cls.from_dict(body)
    except Exception as exc:
        msg = f"{log_label} {cid!r} fails roundtrip: {exc}"
        raise ValueError(msg) from exc
    options = orjson.OPT_APPEND_NEWLINE
    if sort_keys:
        options |= orjson.OPT_SORT_KEYS
    body_path = body_dir / f"{cid}.json"
    body_path.write_bytes(orjson.dumps(body, option=options))


def swap_split_catalog_in(
    *,
    next_bodies: Path,
    live_bodies: Path,
    index_payload: dict[str, Any],
    live_index: Path,
    index_cls: type[_FromDict] | None = None,
    index_entries_key: str | None = None,
    sort_keys: bool = False,
) -> None:
    """Swap a freshly-written next-bodies dir + index into place atomically.

    Index lands at a sibling ``.json.next`` first so a partial write
    can't leave the live file truncated; the bodies-dir swap is
    rmtree + rename (sub-millisecond window); the index swap is
    ``Path.replace`` which is atomic. Between the two swaps the live
    index briefly points at the old id set against the new bodies;
    the runtime loader degrades gracefully across that window
    (missing body files log a warning, new ids aren't yet listed).

    Pass ``index_cls`` + ``index_entries_key`` to roundtrip-validate
    every slim entry in ``index_payload[index_entries_key]`` before
    the swap — catches a sync-time omit_default bug that would ship
    a wire shape the runtime loader rejects.
    """
    if index_cls is not None and index_entries_key is not None:
        for entry in index_payload[index_entries_key]:
            index_cls.from_dict(entry)
    options = orjson.OPT_APPEND_NEWLINE
    if sort_keys:
        options |= orjson.OPT_SORT_KEYS
    next_index = live_index.with_suffix(".json.next")
    next_index.write_bytes(orjson.dumps(index_payload, option=options))
    if live_bodies.exists():
        shutil.rmtree(live_bodies)
    next_bodies.rename(live_bodies)
    next_index.replace(live_index)
