"""Raise the pyproject ``esphome`` floor to a newly-synced stable release."""

from __future__ import annotations

import re
import sys
from pathlib import Path

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"
_STABLE_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_FLOOR_RE = re.compile(r'"esphome>=(\d+)\.(\d+)\.(\d+)"')


def bump_floor(version: str, pyproject: Path = _PYPROJECT) -> str | None:
    """
    Rewrite the ``esphome>=`` floor in *pyproject* to *version*.

    A prerelease or a version at or below the current floor is skipped
    (returns ``None``); a rewrite returns the old floor. The catalog is
    generated against one esphome version and the sync scripts refuse a
    mismatched install, so a stable catalog bump makes that release the
    oldest one that actually works.
    """
    new = _parse_stable(version)
    if new is None:
        print(f"floor: skipping {version} (not a stable release)")
        return None
    text = pyproject.read_text(encoding="utf-8")
    floors = _FLOOR_RE.finditer(text)
    match = next(floors, None)
    if match is None or next(floors, None) is not None:
        raise SystemExit(f"floor: expected exactly one esphome>= floor in {pyproject}")
    old = ".".join(match.groups())
    if new <= tuple(int(part) for part in match.groups()):
        print(f"floor: skipping {version} (floor already at esphome>={old})")
        return None
    pyproject.write_text(text.replace(match[0], f'"esphome>={version}"'), encoding="utf-8")
    print(f"floor: esphome>={old} -> esphome>={version}")
    return old


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: bump_esphome_floor.py <esphome-version>", file=sys.stderr)
        return 2
    bump_floor(argv[0])
    return 0


def _parse_stable(version: str) -> tuple[int, int, int] | None:
    match = _STABLE_RE.match(version)
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
