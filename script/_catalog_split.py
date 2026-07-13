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
    "dumps_envelope_entries_per_line",
    "dumps_map_entry_per_line",
    "emit_body_with_roundtrip",
    "is_unsafe_catalog_id",
    "prepare_next_bodies_dir",
    "swap_split_catalog_in",
]


class _FromDict(Protocol):
    """Protocol for mashumaro dataclasses with a ``from_dict`` classmethod."""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Any: ...


def _dumps_compact_sorted(value: Any) -> bytes:
    return orjson.dumps(value, option=orjson.OPT_SORT_KEYS)


def dumps_envelope_entries_per_line(payload: dict[str, Any]) -> bytes:
    """
    Dump *payload* as JSON with each item of every top-level list on its own line.

    Non-list keys land compact, one per line, sorted; each list keeps its
    order (an empty one stays inline). Deterministic sorted-key orjson
    fragments, trailing newline, no trailing whitespace.
    """
    lines = [b"{"]
    top_keys = sorted(payload)
    for i, key in enumerate(top_keys):
        key_prefix = _dumps_compact_sorted(key) + b":"
        tail = b"," if i < len(top_keys) - 1 else b""
        entries = payload[key]
        if not isinstance(entries, list) or not entries:
            lines.append(key_prefix + _dumps_compact_sorted(entries) + tail)
            continue
        lines.append(key_prefix + b"[")
        for j, entry in enumerate(entries):
            entry_tail = b"," if j < len(entries) - 1 else b""
            lines.append(_dumps_compact_sorted(entry) + entry_tail)
        lines.append(b"]" + tail)
    lines.append(b"}")
    return b"\n".join(lines) + b"\n"


def dumps_map_entry_per_line(payload: dict[str, Any]) -> bytes:
    """
    Dump a JSON object with each top-level key/value pair on its own line.

    Keys sorted, values compact sorted-key orjson; an empty map stays ``{}``.
    """
    if not payload:
        return b"{}\n"
    body = b",\n".join(
        _dumps_compact_sorted(key) + b":" + _dumps_compact_sorted(payload[key])
        for key in sorted(payload)
    )
    return b"{\n" + body + b"\n}\n"


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
) -> None:
    """Swap a freshly-written next-bodies dir + index into place atomically.

    Index lands at a sibling ``.json.next`` first so a partial write
    can't leave the live file truncated; the bodies-dir swap is
    rmtree + rename (sub-millisecond window); the index swap is
    ``Path.replace`` which is atomic. Between the two swaps the live
    index briefly points at the old id set against the new bodies;
    the runtime loader degrades gracefully across that window
    (missing body files log a warning, new ids aren't yet listed).

    The index is written via :func:`dumps_envelope_entries_per_line`
    with each entry list one entry per line. Pass ``index_cls`` to
    roundtrip-validate every slim entry in the payload's lists before
    the swap — catches a sync-time omit_default bug that would ship a
    wire shape the runtime loader rejects.
    """
    if index_cls is not None:
        for value in index_payload.values():
            if isinstance(value, list):
                for entry in value:
                    index_cls.from_dict(entry)
    index_bytes = dumps_envelope_entries_per_line(index_payload)
    next_index = live_index.with_suffix(".json.next")
    next_index.write_bytes(index_bytes)
    if live_bodies.exists():
        shutil.rmtree(live_bodies)
    next_bodies.rename(live_bodies)
    next_index.replace(live_index)
