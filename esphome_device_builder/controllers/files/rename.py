"""Finding and rewriting the ``!include`` references to a package YAML."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import NamedTuple

import yaml
from esphome import util
from yaml.tokens import (
    BlockEndToken,
    BlockMappingStartToken,
    BlockSequenceStartToken,
    FlowMappingEndToken,
    FlowMappingStartToken,
    FlowSequenceEndToken,
    FlowSequenceStartToken,
    KeyToken,
    ScalarToken,
    TagToken,
    Token,
    ValueToken,
)

from ...constants import SECRETS_FILENAMES, is_secrets_file
from ...helpers.yaml import FastestSafeLoader
from .paths import dependency_key, list_archived_devices, list_package_files

_OPENERS = (
    BlockMappingStartToken,
    BlockSequenceStartToken,
    FlowMappingStartToken,
    FlowSequenceStartToken,
)
_CLOSERS = (BlockEndToken, FlowMappingEndToken, FlowSequenceEndToken)


class RenamePlan(NamedTuple):
    """
    What renaming a file changes.

    ``texts`` maps each changed config-relative YAML to its new text and
    ``originals`` to its current one. ``remaining`` lists the scanned files
    that may still reference the old file after the rewrite: they name it,
    or have an unreadable or substitution-built include target. ``stale``
    is the part of it that certainly still includes the old file.
    """

    texts: dict[str, str]
    originals: dict[str, str]
    remaining: set[str]
    stale: set[str]


class Referrers(NamedTuple):
    """The files including a path directly, and those of them ``files/rename`` can update."""

    referenced_by: list[str]
    rewritable: list[str]


class _Target(NamedTuple):
    """
    An include target and where its text sits in the file.

    ``value`` is None when the target can't be read (an alias, a tag, a
    collection, or a file that isn't valid YAML); ``span`` is None when the
    source text isn't the value verbatim (escapes, a scalar spanning lines).
    """

    value: str | None
    span: tuple[int, int] | None


class _Doc(NamedTuple):
    """A YAML file's include targets and the values of all its scalars."""

    targets: list[_Target]
    scalars: list[str]


def rewrite_includes(text: str, *, folder: str, old: str, new: str) -> str:
    """
    Return *text*, read from a file in *folder*, with includes of *old* pointing at *new*.

    *old* and *new* share a folder, so only the file name in each reference
    changes. Substitution-built, absolute and out-of-tree references are left alone.
    """
    doc = _parse(text)
    if doc is None:
        return text
    new_name = posixpath.basename(new)
    spans = sorted(t.span for t in _includes_of(doc, folder, old) if t.span is not None)
    # Right to left, so earlier spans keep their offsets.
    for start, end in reversed(spans):
        name_start = end - len(posixpath.basename(text[start:end]))
        text = text[:name_start] + new_name + text[end:]
    return text


def plan_rename(
    config_dir: Path, old: str, new: str, skip: frozenset[str] = frozenset()
) -> RenamePlan:
    """Plan renaming *old* to *new* across the scanned YAMLs, leaving *skip* alone. Blocking."""
    texts: dict[str, str] = {}
    originals: dict[str, str] = {}
    remaining: set[str] = set()
    stale: set[str] = set()
    # A case-folding filesystem (Windows) loads ``Base.yaml`` for ``base.yaml``.
    flags = re.IGNORECASE if dependency_key("A") != "A" else 0
    old_name = re.compile(
        r"(?<![\w.-])" + re.escape(posixpath.basename(old)) + r"(?![\w.-])", flags
    )
    for rel, folder in _candidates(config_dir):
        if rel in skip or (text := _read(config_dir / rel)) is None:
            continue
        # Secrets files are only checked: rewriting them would need the secrets lock.
        updated = (
            text
            if is_secrets_file(rel)
            else rewrite_includes(text, folder=folder, old=old, new=new)
        )
        if updated != text:
            texts[rel], originals[rel] = updated, text
        doc = _parse(updated)
        if doc is None or _may_still_name(doc, old_name):
            remaining.add(rel)
        if doc is not None and any(_includes_of(doc, folder, old)):
            stale.add(rel)
    return RenamePlan(texts, originals, remaining, stale)


def find_referrers(config_dir: Path, path: str) -> Referrers:
    """
    Return the scanned YAMLs whose ``!include`` names *path*, sorted. Blocking.

    ``rewritable`` are the ones ``plan_rename`` rewrites for *path*; secrets
    files are left out. Best effort: a substitution-built include isn't resolved.
    """
    found: list[str] = []
    rewritable: list[str] = []
    for rel, folder in _candidates(config_dir):
        if is_secrets_file(rel) or (text := _read(config_dir / rel)) is None:
            continue
        targets = list(_includes_of(doc, folder, path)) if (doc := _parse(text)) else []
        if targets:
            found.append(rel)
        if any(t.span is not None for t in targets):
            rewritable.append(rel)
    return Referrers(sorted(found), sorted(rewritable))


def _includes_of(doc: _Doc, folder: str, path: str) -> Iterator[_Target]:
    """Yield the targets in *doc*, read from a file in *folder*, that resolve to *path*."""
    key = dependency_key(path)
    return (
        t
        for t in doc.targets
        if t.value is not None
        and (resolved := _resolve(folder, t.value)) is not None
        and dependency_key(resolved) == key
    )


def _may_still_name(doc: _Doc, old_name: re.Pattern[str]) -> bool:
    """Whether *doc* has an unreadable or substitution-built include, or names *old_name*."""
    return any(t.value is None or "$" in t.value for t in doc.targets) or any(
        old_name.search(value) for value in doc.scalars
    )


def _parse(text: str) -> _Doc | None:
    """Read *text*'s include targets, spans into *text*; None when its syntax isn't valid YAML."""
    # libyaml doesn't count a BOM in token positions and PyYAML does; leave it out of both.
    body = text.removeprefix("\ufeff")
    try:
        tokens = list(yaml.scan(body, Loader=FastestSafeLoader))
        # Tokenizing alone accepts truncated structures (``{ ?``); the parser doesn't.
        for _ in yaml.parse(body, Loader=FastestSafeLoader):
            pass
    except yaml.YAMLError:
        return None
    offset = len(text) - len(body)
    targets = [
        t._replace(span=(t.span[0] + offset, t.span[1] + offset)) if t.span else t
        for t in _targets(tokens, body)
    ]
    return _Doc(targets, [t.value for t in tokens if isinstance(t, ScalarToken)])


def _targets(tokens: list[Token], text: str) -> Iterator[_Target]:
    """
    Yield the target of every ``!include`` in *tokens*, read from *text*.

    A scalar (``!include path``), or the ``file`` key of the include's own
    mapping, flow or block, in any key order; a ``file`` nested deeper
    (``vars: {file: …}``) is not the target.
    """
    for i, token in enumerate(tokens):
        if isinstance(token, TagToken) and token.value == ("!", "include"):
            target = _include_target(tokens, i + 1, text)
            if target is not None:
                yield target


def _include_target(tokens: list[Token], i: int, text: str) -> _Target | None:
    """Return the target of the include node at token *i*; None for a mapping without one."""
    node = tokens[i] if i < len(tokens) else None
    if isinstance(node, ScalarToken):
        return _scalar_target(node, text)
    if not isinstance(node, (BlockMappingStartToken, FlowMappingStartToken)):
        return _Target(None, None)
    depth = 0
    for j in range(i, len(tokens)):
        token = tokens[j]
        if isinstance(token, _OPENERS):
            depth += 1
        elif isinstance(token, _CLOSERS):
            depth -= 1
            if depth == 0:
                return None
        elif depth == 1 and isinstance(token, KeyToken) and _is_file_key(tokens, j):
            value = tokens[j + 3] if j + 3 < len(tokens) else None
            if isinstance(value, ScalarToken):
                return _scalar_target(value, text)
            return _Target(None, None)
    return _Target(None, None)  # the mapping never closes


def _is_file_key(tokens: list[Token], i: int) -> bool:
    """Whether the key at token *i* is ``file``, followed by a value."""
    key, colon = tokens[i + 1 : i + 3] if i + 3 <= len(tokens) else (None, None)
    return isinstance(key, ScalarToken) and key.value == "file" and isinstance(colon, ValueToken)


def _scalar_target(token: ScalarToken, text: str) -> _Target:
    start, end = token.start_mark.index, token.end_mark.index
    if token.style in ("'", '"'):
        start, end = start + 1, end - 1
    elif not token.plain:
        return _Target(token.value, None)  # a block scalar
    return _Target(token.value, (start, end) if text[start:end] == token.value else None)


def _resolve(folder: str, ref: str) -> str | None:
    if "$" in ref or ref.startswith("/") or re.match(r"[A-Za-z]:", ref):
        return None
    target = posixpath.normpath(posixpath.join(folder, ref))
    return None if target == ".." or target.startswith("../") else target


def _read(path: Path) -> str | None:
    try:
        return path.read_bytes().decode("utf-8")  # keeps CRLF
    except (OSError, UnicodeDecodeError):
        return None


def _candidates(config_dir: Path) -> Iterable[tuple[str, str]]:
    """Each scanned YAML, with the folder its includes resolve from."""
    for path in util.list_yaml_files([config_dir]):
        yield path.name, ""
    yield from ((name, "") for name in SECRETS_FILENAMES if (config_dir / name).is_file())
    yield from ((rel, posixpath.dirname(rel)) for rel in list_package_files(config_dir))
    # Unarchiving moves a device back to the top level, so its includes resolve from there.
    yield from ((rel, "") for rel in list_archived_devices(config_dir))
