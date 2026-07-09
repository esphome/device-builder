"""
Shared component-catalog reader for the definition scripts.

Stdlib ``json`` only (no ``orjson``) so the light ``validate-definitions``
pre-commit hook env, which runs without the runtime deps, can import it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_component_catalog(index_path: Path, bodies_dir: Path) -> dict[str, dict[str, Any]]:
    """
    Join the slim component index with each per-id body, keyed by component id.

    Reads *index_path* (a ``<catalog>.index.json`` carrying a ``components``
    list) and merges each per-id ``<bodies_dir>/<id>.json`` on top, so every
    entry carries its full body. Callers own the index-missing policy; this
    assumes *index_path* exists.
    """
    raw = json.loads(index_path.read_text(encoding="utf-8"))
    by_id: dict[str, dict[str, Any]] = {}
    for comp in raw.get("components", []):
        cid = comp.get("id")
        if not cid:
            continue
        body_path = bodies_dir / f"{cid}.json"
        if body_path.is_file():
            by_id[cid] = {**comp, **json.loads(body_path.read_text(encoding="utf-8"))}
        else:
            by_id[cid] = comp
    return by_id
