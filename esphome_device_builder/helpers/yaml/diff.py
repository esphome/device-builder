"""Apply the ``YamlDiff`` splice the automation editor commands return."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...models.automations import YamlDiff


def apply_yaml_diff(text: str, diff: YamlDiff) -> str:
    """Splice *diff* (1-indexed ``fromLine`` / ``toLine``, ``replacement``) into *text*."""
    # Lines are counted the way the producers count them: ``splitlines``.
    lines = text.splitlines(keepends=True)
    from_line, to_line = diff.fromLine, diff.toLine
    if not (1 <= from_line <= len(lines) + 1 and from_line - 1 <= to_line <= len(lines)):
        raise ValueError(f"YamlDiff {from_line}..{to_line} is outside {len(lines)} lines")
    head = lines[: from_line - 1]
    # Only the final element of splitlines can lack a terminator: an append after it needs one.
    if (
        diff.replacement
        and from_line == len(lines) + 1
        and head
        and head[-1] == head[-1].rstrip("\r\n")
    ):
        head[-1] += "\n"
    return "".join(head) + diff.replacement + "".join(lines[to_line:])
