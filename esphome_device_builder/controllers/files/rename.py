"""Rewriting ``!include`` references when a package YAML is renamed in its folder."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

from esphome import util

from ...constants import SECRETS_FILENAMES, is_secrets_file
from .paths import list_package_files

# ``!include path`` / ``!include {file: path}``, path optionally quoted. Not
# after a word character, and the path must end there (``base.yaml.backup``
# is another file). Matches inside a quoted string are skipped in ``_swap``.
# shortcut: matches inside block scalars too; parse YAML nodes if that bites.
_TAG = r"""(?<![\w"'])!include"""
_PATH = r"""(["']?)([^\s"'{},#]+\.(?i:ya?ml))\1(?=[\s"'},]|$)"""
_INCLUDE_RE = re.compile(_TAG + r"\s+(?:\{\s*file:\s*)?" + _PATH)
# Block form: ``!include`` ending the line, ``file: path`` on a deeper line.
_BARE_TAG_RE = re.compile(_TAG + r"\s*$")
# An ``!include`` whose path is built from a ``${substitution}``; never rewritten.
_DYNAMIC_RE = re.compile(_TAG + r"""\s+(?:\{\s*file:\s*)?["']?[^\s"',#]*\$""")
# Any ``file:`` value built from a substitution, for the block form (conservative).
_DYNAMIC_FILE_RE = re.compile(r"""(?<![\w-])file:\s*["']?[^\s"',#]*\$""")
_FILE_KEY_RE = re.compile(r"\s*file:\s*" + _PATH)


class RenamePlan(NamedTuple):
    """
    What renaming a file changes.

    ``texts`` maps each changed config-relative YAML to its new text and
    ``originals`` to its current one. ``remaining`` lists the scanned files
    that may still reference the old file after the rewrite: they name it
    outside a comment or have a substitution-built ``!include``.
    """

    texts: dict[str, str]
    originals: dict[str, str]
    remaining: set[str]


def rewrite_includes(text: str, *, folder: str, old: str, new: str) -> str:
    """
    Return *text*, read from a file in *folder*, with includes of *old* pointing at *new*.

    *old* and *new* share a folder, so only the file name in each reference
    changes. Substitution-built, absolute and out-of-tree references are left alone.
    """
    new_name = posixpath.basename(new)
    out: list[str] = []
    bare_indent: int | None = None

    def _swap(line: str, matches: Iterable[re.Match[str] | None]) -> str:
        parts: list[str] = []
        last = 0
        for match in matches:
            if match is None or _resolve(folder, match.group(2)) != old:
                continue
            before = line[: match.start()]
            if before.count('"') % 2 or before.count("'") % 2:
                continue  # inside a quoted string; ``remaining`` still flags it
            name_start = match.end(2) - len(posixpath.basename(match.group(2)))
            parts += [line[last:name_start], new_name]
            last = match.end(2)
        return "".join(parts) + line[last:] if parts else line

    for raw in text.splitlines(keepends=True):
        code = _code(raw)
        line = raw
        if bare_indent is not None and code.strip():
            if _indent(raw) > bare_indent:
                line = _swap(line, [_FILE_KEY_RE.match(code)])
            bare_indent = None
        line = _swap(line, _INCLUDE_RE.finditer(code))
        if _BARE_TAG_RE.search(code.rstrip()):
            bare_indent = _indent(raw)
        out.append(line)
    return "".join(out)


def plan_rename(config_dir: Path, old: str, new: str) -> RenamePlan:
    """Plan renaming *old* to *new* across the device YAMLs and packages. Blocking."""
    texts: dict[str, str] = {}
    originals: dict[str, str] = {}
    remaining: set[str] = set()
    old_name = re.compile(r"(?<![\w.-])" + re.escape(posixpath.basename(old)) + r"(?![\w.-])")
    for rel in _candidates(config_dir):
        try:
            text = (config_dir / rel).read_bytes().decode("utf-8")  # keeps CRLF
        except (OSError, UnicodeDecodeError):
            continue
        # Secrets files are only checked: rewriting them would need the secrets lock.
        updated = (
            text
            if is_secrets_file(rel)
            else rewrite_includes(text, folder=posixpath.dirname(rel), old=old, new=new)
        )
        if updated != text:
            texts[rel], originals[rel] = updated, text
        codes = [_code(line) for line in updated.splitlines()]
        dynamic = (_DYNAMIC_RE, _DYNAMIC_FILE_RE)
        if any(old_name.search(code) or any(r.search(code) for r in dynamic) for code in codes):
            remaining.add(rel)
    return RenamePlan(texts, originals, remaining)


def _code(line: str) -> str:
    """Return *line* up to its comment; a ``#`` inside a quoted scalar starts none."""
    quote, prev = "", " "
    for i, ch in enumerate(line):
        if quote:
            if ch == quote and not (quote == '"' and prev == "\\"):
                quote = ""
        elif ch in "\"'" and (prev.isspace() or prev in ":[{,"):
            quote = ch
        elif ch == "#" and prev.isspace():
            return line[:i]
        prev = ch
    return line


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _resolve(folder: str, ref: str) -> str | None:
    if "$" in ref or ref.startswith("/") or re.match(r"[A-Za-z]:", ref):
        return None
    target = posixpath.normpath(posixpath.join(folder, ref))
    return None if target == ".." or target.startswith("../") else target


def _candidates(config_dir: Path) -> Iterable[str]:
    for path in util.list_yaml_files([config_dir]):
        yield path.name
    yield from (name for name in SECRETS_FILENAMES if (config_dir / name).is_file())
    yield from list_package_files(config_dir)
