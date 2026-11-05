"""``--check`` reporting for ``script/sync_esphome_devices.py``."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, Protocol

NOT_A_DEVICE_PAGE = "no device page (index.md with frontmatter) in that folder"


class _Skip(Protocol):
    folder_name: str
    reason: str


def check_results(
    requested: list[str] | None, imported: set[str], skipped: Iterable[_Skip]
) -> list[dict[str, Any]]:
    """One ``{device, imported, reason}`` row per requested folder, else per folder seen."""
    reasons = {s.folder_name: s.reason for s in skipped}
    devices = requested or sorted(imported | reasons.keys())
    rows: list[dict[str, Any]] = []
    for device in dict.fromkeys(devices):
        if device in imported:
            rows.append({"device": device, "imported": True, "reason": None})
        else:
            reason = reasons.get(device, NOT_A_DEVICE_PAGE)
            rows.append({"device": device, "imported": False, "reason": reason})
    return rows


def print_check_results(rows: list[dict[str, Any]], *, as_json: bool) -> None:
    """Print *rows* as JSON, or one ``ok`` / ``skip`` line per device."""
    if as_json:
        print(json.dumps(rows, indent=2))
        return
    for row in rows:
        if row["imported"]:
            print(f"ok    {row['device']}")
        else:
            print(f"skip  {row['device']}: {row['reason']}")
