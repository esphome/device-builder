"""Classify a Dependabot PR diff as a ``major``, ``minor`` or ``patch`` update."""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator

UNKNOWN = "unknown"
_LEVELS: tuple[str, ...] = ("patch", "minor", "major")
_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")


def update_type(diff: str) -> str:
    """Return the largest version jump in *diff*, or ``unknown`` when it can't be read."""
    level = -1
    try:
        for removed, added in _changed_pairs(diff):
            old = _VERSION_RE.findall(removed)
            new = _VERSION_RE.findall(added)
            if len(old) != len(new):
                return UNKNOWN
            for old_version, new_version in zip(old, new, strict=True):
                level = max(level, _jump(old_version, new_version))
    except ValueError:
        return UNKNOWN
    return _LEVELS[level] if level >= 0 else UNKNOWN


def main() -> None:
    """Print the update type of the diff on stdin."""
    print(update_type(sys.stdin.read()))


def _changed_pairs(diff: str) -> Iterator[tuple[str, str]]:
    """Yield each removed line with the added line that replaced it; raise on uneven blocks."""
    removed: list[str] = []
    added: list[str] = []
    in_header = False
    for line in [*diff.splitlines(), ""]:
        if line.startswith("diff "):
            in_header = True
        elif line.startswith("@@"):
            in_header = False
        if in_header or line.startswith("\\"):
            continue
        if line.startswith("-"):
            removed.append(line[1:])
        elif line.startswith("+"):
            added.append(line[1:])
        else:
            if len(removed) != len(added):
                raise ValueError("unpaired change block")
            yield from zip(removed, added, strict=True)
            removed, added = [], []


def _jump(old: str, new: str) -> int:
    """Return the index into ``_LEVELS`` of the jump from *old* to *new*, or -1 if equal."""
    old_parts = [int(part) for part in old.split(".")]
    new_parts = [int(part) for part in new.split(".")]
    width = max(len(old_parts), len(new_parts))
    old_parts += [0] * (width - len(old_parts))
    new_parts += [0] * (width - len(new_parts))
    for index, (old_part, new_part) in enumerate(zip(old_parts, new_parts, strict=True)):
        if old_part != new_part:
            return max(len(_LEVELS) - 1 - index, 0)
    return -1


if __name__ == "__main__":
    main()
