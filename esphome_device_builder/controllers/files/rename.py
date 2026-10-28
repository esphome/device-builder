"""Rewriting ``!include`` references when a package YAML moves."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable
from pathlib import Path

from esphome import util

from ...constants import is_secrets_file
from .paths import list_package_files

# ``!include path`` / ``!include {file: path}``, path optionally quoted.
_INCLUDE_RE = re.compile(r"""!include\s+(?:\{\s*file:\s*)?(["']?)([^\s"'{},#]+\.ya?ml)\1""")
_COMMENT_RE = re.compile(r"(^|\s)#")


def rewrite_includes(text: str, *, read_dir: str, write_dir: str, old: str, new: str) -> str:
    """
    Return *text* with its include references to *old* pointing at *new*.

    References resolve against *read_dir* (the file's folder before the move)
    and are written relative to *write_dir* (its folder after); when the two
    differ every resolvable reference is rebased. Substitution-built,
    absolute and out-of-tree references are left alone.
    """
    moved = read_dir != write_dir

    def _rewrite(line: str) -> str:
        comment = _COMMENT_RE.search(line)
        code_end = comment.start() if comment else len(line)
        parts: list[str] = []
        last = 0
        for match in _INCLUDE_RE.finditer(line, 0, code_end):
            ref = match.group(2)
            target = _resolve(read_dir, ref)
            if target is None or (target != old and not moved):
                continue
            new_ref = posixpath.relpath(new if target == old else target, write_dir or ".")
            if new_ref == ref:
                continue
            parts += [line[last : match.start(2)], new_ref]
            last = match.end(2)
        return "".join(parts) + line[last:] if parts else line

    return "".join(_rewrite(line) for line in text.splitlines(keepends=True))


def plan_rename(config_dir: Path, old: str, new: str) -> dict[str, str]:
    """
    Map each config-relative YAML whose text changes when *old* moves to *new* to that text.

    Covers device YAMLs and packages; the moved file is keyed by *old*. Blocking.
    """
    plan: dict[str, str] = {}
    for rel in _candidates(config_dir):
        path = config_dir / rel
        try:
            text = path.read_text("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        folder = posixpath.dirname(rel)
        write_dir = posixpath.dirname(new) if rel == old else folder
        updated = rewrite_includes(text, read_dir=folder, write_dir=write_dir, old=old, new=new)
        if updated != text:
            plan[rel] = updated
    return plan


def _resolve(folder: str, ref: str) -> str | None:
    if "$" in ref or ref.startswith("/") or re.match(r"[A-Za-z]:", ref):
        return None
    target = posixpath.normpath(posixpath.join(folder, ref))
    return None if target == ".." or target.startswith("../") else target


def _candidates(config_dir: Path) -> Iterable[str]:
    for path in util.list_yaml_files([config_dir]):
        if not is_secrets_file(path):
            yield path.name
    yield from list_package_files(config_dir)
