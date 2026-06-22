#!/usr/bin/env python3
"""
One-step board update for contributors.

Edit a board's ``manifest.yaml``, then run this. It regenerates that
board's catalog JSON, validates it, and prints what to commit, so you
don't have to remember ``sync_boards.py`` + ``validate_definitions.py``
or which ESPHome the catalog is pinned to (``sync_boards.py`` enforces
that and this surfaces its error).

    python script/update_board.py                 # auto-detect the board you edited
    python script/update_board.py BOARD_ID        # name it explicitly

Run it with the project venv's Python (``.venv/bin/python`` or an
activated venv); it reuses that interpreter for every step.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_DIR = Path(__file__).resolve().parent
# Repo-relative POSIX path: used as a git pathspec (git resolves these against
# the repo root, cross-platform) and shown in messages as the on-disk location.
_BOARDS_REL = "esphome_device_builder/definitions/boards"
_BOARDS_DIR = _REPO_ROOT / _BOARDS_REL


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate, validate, and report one board's catalog files."
    )
    parser.add_argument(
        "board",
        nargs="?",
        help=f"Board id (the folder name under {_BOARDS_REL}/). "
        "Omit to auto-detect the manifest you edited.",
    )
    args = parser.parse_args()

    board_id = args.board or _detect_edited_board()
    if board_id is None:
        return 1
    if not (_BOARDS_DIR / board_id / "manifest.yaml").is_file():
        print(
            f"update_board: no manifest at {_BOARDS_REL}/{board_id}/manifest.yaml",
            file=sys.stderr,
        )
        return 1

    print(f"==> Updating board: {board_id}")
    if not _run("Regenerating catalog JSON", "sync_boards.py", board_id):
        return 1
    if not _run("Validating definitions", "validate_definitions.py"):
        return 1

    print(f"\nDone. {board_id} is ready. Commit the changed files:", flush=True)
    subprocess.run(
        ["git", "status", "--short", "esphome_device_builder/definitions"],
        cwd=_REPO_ROOT,
        close_fds=False,
        check=False,
    )
    return 0


def _detect_edited_board() -> str | None:
    """Return the single board id with an edited manifest, or None (and explain)."""
    result = subprocess.run(
        ["git", "status", "--porcelain", "--", _BOARDS_REL],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        close_fds=False,
        check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        print("update_board: git status failed; pass a board id explicitly.", file=sys.stderr)
        return None
    ids = sorted(
        {
            board_id
            for line in result.stdout.splitlines()
            if (board_id := _board_id_from_status_line(line)) is not None
        }
    )
    if len(ids) == 1:
        return ids[0]
    if not ids:
        print(
            f"update_board: no edited board manifest found. Edit "
            f"{_BOARDS_REL}/<id>/manifest.yaml first, or pass a board id.",
            file=sys.stderr,
        )
        return None
    print(
        "update_board: several boards changed; name one:\n  " + "\n  ".join(ids),
        file=sys.stderr,
    )
    return None


def _board_id_from_status_line(line: str) -> str | None:
    """Pull ``<id>`` from a ``git status --porcelain`` line touching a board manifest."""
    path = line[3:].rsplit(" -> ", maxsplit=1)[-1].strip().strip('"')
    prefix = f"{_BOARDS_REL}/"
    if not path.startswith(prefix) or not path.endswith("/manifest.yaml"):
        return None
    return path[len(prefix) :].split("/", 1)[0]


def _run(label: str, script: str, *script_args: str) -> bool:
    """Run a sibling script with this interpreter, streaming its output."""
    print(f"--> {label} ...", flush=True)
    result = subprocess.run(
        [sys.executable, str(_SCRIPT_DIR / script), *script_args],
        cwd=_REPO_ROOT,
        close_fds=False,
        check=False,
    )
    if result.returncode != 0:
        print(f"FAILED: {label.lower()}", file=sys.stderr)
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
