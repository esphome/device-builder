"""Small user-facing text helpers."""

from __future__ import annotations

from collections.abc import Iterable
from difflib import unified_diff

_DIFF_EXCERPT_LINES = 6  # per side


def summarise(messages: Iterable[str]) -> str:
    """Join up to three non-empty messages with a ``(+N more)`` count, period-trimmed."""
    shown_all = [msg for msg in messages if msg]
    shown = shown_all[:3]
    suffix = f" (+{len(shown_all) - len(shown)} more)" if len(shown_all) > len(shown) else ""
    # esphome messages often end with their own period; the caller's
    # tail brings the sentence break.
    return ("; ".join(shown) + suffix).removesuffix(".")


def same_text(current: str, expected: str) -> bool:
    """Whether *expected* equals *current* ignoring trailing newlines."""
    return current.rstrip("\n") == expected.rstrip("\n")


def diff_excerpt(expected: str, current: str) -> str:
    """Return a unified diff from *expected* to *current* cut to a few lines of each side."""
    lines = list(
        unified_diff(
            expected.splitlines(keepends=True),
            current.splitlines(keepends=True),
            "expected",
            "current",
            n=0,
        )
    )
    budget = {"-": _DIFF_EXCERPT_LINES, "+": _DIFF_EXCERPT_LINES}
    shown: list[str] = []
    pending_hunk: str | None = None
    for position, line in enumerate(lines):
        # The first two records are the file headers, whatever they start with.
        if position >= 2 and line.startswith("@@"):
            pending_hunk = line  # shown only once a budgeted line under it is
            continue
        side = line[:1] if position >= 2 and line[:1] in budget else ""
        if side and budget[side] == 0:
            continue
        if side:
            budget[side] -= 1
        if pending_hunk is not None:
            shown.append(pending_hunk if pending_hunk.endswith("\n") else pending_hunk + "\n")
            pending_hunk = None
        shown.append(line if line.endswith("\n") else line + "\n")
    hidden = len(lines) - len(shown)
    return "".join(shown) + (f"... {hidden} more diff lines\n" if hidden else "")
