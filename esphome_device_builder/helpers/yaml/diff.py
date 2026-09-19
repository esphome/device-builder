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
    if to_line < from_line - 1:
        raise ValueError(f"YamlDiff {from_line}..{to_line} is inverted")
    if from_line < 1 or to_line > len(lines):
        raise ValueError(f"YamlDiff {from_line}..{to_line} is outside {len(lines)} lines")
    head = "".join(lines[: from_line - 1])
    # Only the text's last line can lack a terminator; an append after it starts a new line.
    if diff.replacement and head and not _ends_line(head[-1]):
        head += "\r\n" if "\r\n" in head else "\n"
    return head + diff.replacement + "".join(lines[to_line:])


def _ends_line(char: str) -> bool:
    """Return True when *char* is a line boundary to ``str.splitlines``."""
    return len(f"{char}x".splitlines()) == 2
