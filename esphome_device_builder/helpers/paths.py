"""Shared path-containment guard for write / delete / serve targets."""

from __future__ import annotations

from pathlib import Path


class PathEscapeError(ValueError):
    """*target* resolved outside the trusted *root*, or couldn't be resolved at all."""


def resolve_under_root(target: Path, root: Path) -> Path:
    """
    Resolve *target* and require it stays under resolved *root*.

    Collapses ``..`` / symlinks via ``Path.resolve()`` before the
    containment check; returns the resolved target. A target that
    can't be resolved at all (embedded NUL) fails closed as an
    escape. Blocking (``resolve`` walks the filesystem) — call from
    executor threads.
    """
    # Explicit NUL gate: POSIX ``resolve()`` raises ``ValueError`` on an
    # embedded NUL but Windows swallows it, so the check can't ride the
    # except below on every platform.
    if "\x00" in str(target):
        raise PathEscapeError(f"{str(target)!r} contains a NUL byte")
    try:
        resolved = target.resolve()
    except ValueError as err:
        raise PathEscapeError(f"{str(target)!r} is unresolvable: {err}") from err
    if not resolved.is_relative_to(root.resolve()):
        # ``!r`` so control characters in a user-controlled filename
        # can't inject into logs / error strings built from this message.
        raise PathEscapeError(f"{str(target)!r} resolves outside {str(root)!r}")
    return resolved
