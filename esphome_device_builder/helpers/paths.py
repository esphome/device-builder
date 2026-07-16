"""Shared path-containment guard for write / delete / serve targets."""

from __future__ import annotations

from pathlib import Path


class PathEscapeError(ValueError):
    """*target* resolved outside the trusted *root*."""


def resolve_under_root(target: Path, root: Path) -> Path:
    """
    Resolve *target* and require it stays under resolved *root*.

    Collapses ``..`` / symlinks via ``Path.resolve()`` before the
    containment check; returns the resolved target. A target that
    can't be resolved at all (embedded NUL) fails closed as an
    escape. Blocking (``resolve`` walks the filesystem) — call from
    executor threads.
    """
    try:
        resolved = target.resolve()
    except ValueError as err:
        raise PathEscapeError(f"{target!r} is unresolvable: {err}") from err
    if not resolved.is_relative_to(root.resolve()):
        raise PathEscapeError(f"{target} resolves outside {root}")
    return resolved
