"""
Source-location helpers for the parser.

Map ruamel ``lc`` (line/column) metadata to 1-indexed line ranges so
the navigator maps a click to the right YAML span without re-parsing,
plus the round-trip slice dump that backs each entry's ``raw_yaml``.
"""

from __future__ import annotations

from io import StringIO
from typing import Any

from ._yaml import make_yaml


def _pretty_name(key: str) -> str:
    """Title-case an ``on_x_y`` key for display labels."""
    return key.replace("_", " ").title()


def _key_range(mapping: Any, key: str) -> tuple[int, int]:
    """Return the 1-indexed line range covering ``mapping[key]``."""
    lc = getattr(mapping, "lc", None)
    if lc is None or not getattr(lc, "data", None) or key not in lc.data:
        return 1, 1
    key_line, _key_col, _val_line, _val_col = lc.data[key]
    start = key_line + 1
    end = _estimate_end_line(mapping[key], start)
    return start, end


def _item_range(seq: Any, idx: int) -> tuple[int, int]:
    """Return the 1-indexed line range for the *idx*'th list item."""
    lc = getattr(seq, "lc", None)
    if lc is None or not getattr(lc, "data", None) or idx not in lc.data:
        return 1, 1
    # Use the dash-line index so leading blank / comment lines
    # don't shift the start.
    dash_line = lc.data[idx][0]
    start = dash_line + 1
    end = _estimate_end_line(seq[idx], start)
    return start, end


def _estimate_end_line(value: Any, start: int) -> int:
    """Walk a sub-tree and pick the largest ``lc.line`` we observe."""
    max_line = start
    stack: list[Any] = [value]
    while stack:
        node = stack.pop()
        lc = getattr(node, "lc", None)
        if lc is not None and getattr(lc, "line", None) is not None:
            max_line = max(max_line, lc.line + 1)
        if isinstance(node, dict):
            stack.extend(node.values())
            data = getattr(lc, "data", None) if lc else None
            if data:
                for entry in data.values():
                    # ruamel entries are (key_line, key_col, val_line, val_col)
                    if isinstance(entry, (list, tuple)) and len(entry) >= 3:
                        max_line = max(max_line, entry[2] + 1)
        elif isinstance(node, list):
            stack.extend(node)
            # ruamel sequence ``lc.data`` entries are 2-tuples
            # (dash_line, dash_col) — they don't carry a value-line
            # we could use, so we rely on the recursive walk into
            # the inner mapping for the actual end line.
    return max_line


def _dump_slice(value: Any) -> str:
    """Serialise *value* through the round-trip emitter as a YAML string."""
    yaml = make_yaml()
    buf = StringIO()
    yaml.dump(value, buf)
    return buf.getvalue()
