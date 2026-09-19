"""Apply the ``YamlDiff`` splice the automation editor commands return."""

from __future__ import annotations

from typing import Any


def apply_yaml_diff(text: str, diff: dict[str, Any]) -> str:
    """Splice *diff* (1-indexed ``fromLine`` / ``toLine``, ``replacement``) into *text*."""
    # Lines are counted the way the producers count them: ``splitlines``.
    lines = text.splitlines(keepends=True)
    from_line, to_line = diff["fromLine"], diff["toLine"]
    if not (1 <= from_line <= len(lines) + 1 and from_line - 1 <= to_line <= len(lines)):
        raise ValueError(f"YamlDiff {from_line}..{to_line} is outside {len(lines)} lines")
    replacement: str = diff["replacement"]
    head = lines[: from_line - 1]
    if replacement and head and head[-1] == head[-1].rstrip("\r\n"):
        head[-1] += "\n"  # the kept last line had no terminator
    return "".join(head) + replacement + "".join(lines[to_line:])
