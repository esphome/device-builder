"""Apply the ``YamlDiff`` splice the automation editor commands return."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...models.automations import YamlDiff


def apply_yaml_diff(text: str, diff: YamlDiff) -> str:
    """Apply *diff* to *text*; raise ``ValueError`` when it falls outside the text."""
    # splitlines, not split("\n"): the diff's line numbers come from splitlines.
    lines = text.splitlines(keepends=True)
    from_line, to_line = diff.fromLine, diff.toLine
    if not 1 <= from_line <= to_line + 1 <= len(lines) + 1:
        raise ValueError(f"YamlDiff {from_line}..{to_line} is outside {len(lines)} lines")
    head = "".join(lines[: from_line - 1])
    # An append after a final unterminated line needs the boundary it lacks.
    if diff.replacement and from_line > len(lines) and text and not text.endswith(("\n", "\r")):
        head += "\n"
    return head + diff.replacement + "".join(lines[to_line:])
