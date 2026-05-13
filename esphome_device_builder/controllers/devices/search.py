"""``yaml/search`` WS command body."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ._yaml_search import (
    DEFAULT_CONTEXT_LINES,
    MAX_CONTEXT_LINES,
    search_yaml_devices,
)

if TYPE_CHECKING:
    from .controller import DevicesController

# Per-file match cap; each device contributes at most this many
# lines so a chatty match (a query of ``:`` against a deeply
# nested config) doesn't drown hits in other devices. The
# dropdown's overall hit count is capped at the caller-supplied
# ``max_results`` on top of this.
_YAML_SEARCH_PER_FILE_MATCH_CAP = 5


async def search_yaml(
    controller: DevicesController,
    *,
    query: str,
    max_results: int,
    case_sensitive: bool,
    context_lines: int | None,
) -> list[dict]:
    """
    Substring-search every configured device's raw YAML file.

    Empty / whitespace-only queries return ``[]``. Iterates
    the scanner's existing snapshot, not a fresh scan;
    ``context_lines`` clamps to ``[0, MAX_CONTEXT_LINES]``.
    """
    needle_raw = query.strip()
    if not needle_raw:
        return []
    needle = needle_raw if case_sensitive else needle_raw.lower()

    if context_lines is None:
        effective_context_lines = DEFAULT_CONTEXT_LINES
    else:
        # Out-of-range values clamp rather than fall back to
        # the default; a caller passing 10_000 clearly wants
        # "as much as possible" and getting MAX is closer to
        # that intent than silently substituting the default.
        effective_context_lines = max(0, min(context_lines, MAX_CONTEXT_LINES))

    # Global search lock: serialise the I/O-bound walk so two
    # concurrent searches don't double up on stat / read calls
    # against the same fleet.
    async with controller._yaml_search_lock:
        results, live_configurations = await search_yaml_devices(
            devices=controller._scanner.devices,
            cache=controller._yaml_search_cache,
            rel_path=lambda c: Path(controller._db.settings.rel_path(c)),
            needle=needle,
            case_sensitive=case_sensitive,
            max_results=max_results,
            per_file_cap=_YAML_SEARCH_PER_FILE_MATCH_CAP,
            context_lines=effective_context_lines,
        )
        controller._yaml_search_cache.prune(live_configurations)
        return results
