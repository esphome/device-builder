"""Remove ``secrets.yaml`` values from text that leaves the dashboard."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..constants import SECRETS_FILENAMES
from ..models import ErrorCode
from .api import CommandError
from .secrets_state import validate_secrets_content

_LOGGER = logging.getLogger(__name__)

# Shorter values (ports, small ids) would shred unrelated output.
MIN_REDACTED_SECRET_LEN = 6
REDACTED = "<removed>"


def load_secret_mappings(*directories: Path) -> list[dict[Any, Any]]:
    """Parse each secrets file in *directories*; ``UNAVAILABLE`` when one cannot be used."""
    mappings: list[dict[Any, Any]] = []
    candidates = [directory / name for directory in directories for name in SECRETS_FILENAMES]
    for path in dict.fromkeys(candidates):
        try:
            content = path.read_text("utf-8")
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as err:
            raise _unavailable(path, "read", err) from err
        try:
            mappings.append(validate_secrets_content(content, path))
        except ValueError as err:
            raise _unavailable(path, "parsed", err) from err
    return mappings


def redact_secret_values(lines: list[str], mappings: list[dict[Any, Any]]) -> list[str]:
    """Replace every value of credential length from *mappings* with ``<removed>`` in *lines*."""
    # Output arrives one line at a time, so a multi-line value must match by line.
    values = {
        part
        for text in _scalar_leaves(mappings)
        for part in text.splitlines()
        if len(part) >= MIN_REDACTED_SECRET_LEN
    }
    for value in sorted(values, key=len, reverse=True):
        lines = [line.replace(value, REDACTED) for line in lines]
    return lines


def _unavailable(path: Path, problem: str, err: Exception) -> CommandError:
    """Log and build the refusal for a secrets file that could not be *problem*."""
    _LOGGER.warning("%s could not be %s", path.name, problem, exc_info=err)
    return CommandError(ErrorCode.UNAVAILABLE, f"{path.name} could not be {problem}")


def _scalar_leaves(value: Any) -> list[str]:
    """Return every scalar inside *value* as text, walking lists and mappings."""
    if isinstance(value, dict):
        return [leaf for item in value.values() for leaf in _scalar_leaves(item)]
    if isinstance(value, list):
        return [leaf for item in value for leaf in _scalar_leaves(item)]
    if value is None or isinstance(value, bool):
        return []
    return [str(value)]
