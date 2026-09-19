"""Build and apply the ``YamlDiff`` splice the automation editor commands exchange."""

from __future__ import annotations

from ...models.automations import YamlDiff


def splice_lines(
    lines: list[str], *, start: int, end: int, replacement: str
) -> tuple[str, YamlDiff]:
    """Replace ``lines[start:end]`` with *replacement*; return the new text and its diff."""
    head, body, tail = "".join(lines[:start]), replacement, "".join(lines[end:])
    # Only the text's last line can lack a terminator; a splice that reaches it leaves the
    # text unterminated, as the frontend's splice does.
    if not tail and lines and not _ends_line(lines[-1][-1]):
        body = _without_terminator(replacement)
        if not body:
            head = _without_terminator(head)
        elif head and not _ends_line(head[-1]):
            head += "\r\n" if "\r\n" in head else "\n"
    new_text = head + body + tail
    return new_text, YamlDiff(fromLine=start + 1, toLine=end, replacement=replacement)


def apply_yaml_diff(text: str, diff: YamlDiff) -> str:
    """Apply *diff* to *text*; raise ``ValueError`` when it falls outside the text."""
    # splitlines, not split("\n"): the diff's line numbers come from splitlines.
    lines = text.splitlines(keepends=True)
    from_line, to_line = diff.fromLine, diff.toLine
    if to_line < from_line - 1:
        raise ValueError(f"YamlDiff {from_line}..{to_line} is inverted")
    if from_line < 1 or to_line > len(lines):
        raise ValueError(f"YamlDiff {from_line}..{to_line} is outside {len(lines)} lines")
    return splice_lines(lines, start=from_line - 1, end=to_line, replacement=diff.replacement)[0]


def _ends_line(char: str) -> bool:
    """Return True when *char* is a line boundary to ``str.splitlines``."""
    return len(f"{char}x".splitlines()) == 2


def _without_terminator(text: str) -> str:
    """Return *text* without its final line terminator."""
    return text.removesuffix("\n").removesuffix("\r")
