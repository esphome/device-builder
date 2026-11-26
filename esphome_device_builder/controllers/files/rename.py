"""Finding and rewriting the ``!include`` references to a package YAML."""

from __future__ import annotations

import logging
import posixpath
import re
from collections.abc import Iterable, Iterator
from enum import Enum
from pathlib import Path
from typing import NamedTuple

import yaml
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

from ...helpers.config_layout import is_secrets_like_name, iter_referrer_yamls, resolved_rel
from ...helpers.yaml import FastestSafeLoader
from .paths import dependency_key, list_archived_devices

_LOGGER = logging.getLogger(__name__)

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
    can't be read, have an unreadable or substitution-built include target,
    or are a symlink to the old file that the rename leaves dangling.
    ``stale`` is the part of it that certainly still includes the old file,
    or includes it through such a symlink, or through a path that can't be resolved.
    """

    texts: dict[str, str]
    originals: dict[str, str]
    remaining: set[str]
    stale: set[str]


class Referrers(NamedTuple):
    """
    The files including a path directly, and those of them ``files/rename`` can update.

    ``unreadable`` are the files that couldn't be checked: unreadable, or not
    valid YAML while naming the path's file name.
    """

    referenced_by: list[str]
    rewritable: list[str]
    unreadable: list[str]


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


class _Reach(Enum):
    """How an include reaches the file a rename moves."""

    # Names its directory entry: rewriting the file name follows the rename.
    ENTRY = "entry"
    # Loads it through a symlink of another name, which the rename leaves dangling.
    ALIAS = "alias"
    # Lexically names it, but a symlink on the way can't be resolved.
    UNKNOWN = "unknown"


class _Located(NamedTuple):
    """Where a config-relative path's directory entry sits and which file it loads."""

    entry: str | None
    file: str | None


class _Source(NamedTuple):
    """The file a rename moves, located once for every candidate."""

    config_dir: Path
    path: str
    located: _Located


def rewrite_includes(text: str, *, config_dir: Path, folder: str, old: str, new: str) -> str:
    """
    Return *text*, read from a file in *folder*, with includes of *old* pointing at *new*.

    *old* and *new* share a folder, so only the file name in each reference
    changes. Substitution-built, absolute and out-of-tree references are left
    alone, as is a reference whose path, resolved through symlinks in *config_dir*,
    is not *old*'s. Blocking.
    """
    return _rewrite(text, _source(config_dir, old), folder, new)


def _rewrite(text: str, source: _Source, folder: str, new: str) -> str:
    """Return *text* with the references that name *source*'s entry pointing at *new*."""
    doc = _parse(text)
    if doc is None:
        return text
    new_name = posixpath.basename(new)
    spans = sorted(
        t.span
        for t, reach in _reaches(doc, source, folder)
        if reach is _Reach.ENTRY and t.span is not None
    )
    # Right to left, so earlier spans keep their offsets.
    for start, end in reversed(spans):
        # Windows also takes ``\\`` as a separator; elsewhere a matched reference has none.
        name_start = end - len(posixpath.basename(text[start:end].replace("\\", "/")))
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
    old_name = _name_pattern(old)
    source = _source(config_dir, old)
    for rel, folder in _candidates(config_dir):
        if _is_alias(source, rel):
            # Left as it is; the rename leaves it dangling, so its users are reported.
            _LOGGER.warning("%s links to %s; renaming leaves it dangling", rel, old)
            remaining.add(rel)
        if rel in skip:
            continue
        if (text := _read(config_dir / rel)) is None:
            remaining.add(rel)  # can't be checked
            continue
        updated = text if _check_only(config_dir, rel) else _rewrite(text, source, folder, new)
        if updated != text:
            texts[rel], originals[rel] = updated, text
        doc = _parse(updated)
        if doc is None or _may_still_name(doc, old_name):
            remaining.add(rel)
        if doc is not None and any(_reaches(doc, source, folder)):
            stale.add(rel)
    return RenamePlan(texts, originals, remaining, stale)


def find_referrers(config_dir: Path, path: str) -> Referrers:
    """
    Return the scanned YAMLs whose ``!include`` names *path*, sorted. Blocking.

    ``rewritable`` are the ones ``plan_rename`` rewrites for *path*; secrets
    files (``secrets.<x>.yaml`` too) are left out. Best effort: a substitution-built
    include isn't resolved.
    """
    found: list[str] = []
    rewritable: list[str] = []
    unreadable: list[str] = []
    name = _name_pattern(path)
    source = _source(config_dir, path)
    for rel, folder in _candidates(config_dir):
        if is_secrets_like_name(rel):
            continue
        text = _read(config_dir / rel)
        doc = None if text is None else _parse(text)
        if doc is None:
            if text is None or name.search(text):
                unreadable.append(rel)
            continue
        targets = [t for t, reach in _reaches(doc, source, folder) if reach is _Reach.ENTRY]
        if targets:
            found.append(rel)
        if any(t.span is not None for t in targets) and not _check_only(config_dir, rel):
            rewritable.append(rel)
    return Referrers(sorted(found), sorted(rewritable), sorted(unreadable))


def _name_pattern(path: str) -> re.Pattern[str]:
    """Match *path*'s file name as a whole word, in any case where the filesystem folds it."""
    # A case-folding filesystem (Windows) loads ``Base.yaml`` for ``base.yaml``.
    flags = re.IGNORECASE if dependency_key("A") != "A" else 0
    return re.compile(r"(?<![\w.-])" + re.escape(posixpath.basename(path)) + r"(?![\w.-])", flags)


def _reaches(doc: _Doc, source: _Source, folder: str) -> Iterator[tuple[_Target, _Reach]]:
    """Yield each target in *doc*, read from a file in *folder*, that reaches *source*. Blocking."""
    for target in doc.targets:
        if target.value is None or (lexical := _resolve(folder, target.value)) is None:
            continue
        reach = _reach(source, lexical, posixpath.join(folder, dependency_key(target.value)))
        if reach is not None:
            yield target, reach


def _reach(source: _Source, lexical: str, joined: str) -> _Reach | None:
    """
    How the include at config-relative *joined* (*lexical* once normalised) reaches *source*.

    Resolved through symlinks, since ``..`` after a symlinked folder leaves that folder's
    target, not the folder the text names. Blocking.
    """
    try:
        located = _locate(source.config_dir, joined, lexical)
    except OSError:
        # A symlink loop or an unreadable folder on the way: esphome decides.
        return _Reach.UNKNOWN if _same(lexical, source.path) else None
    if _same(located.entry, source.located.entry):
        return _Reach.ENTRY
    if _same(located.file, source.located.file):
        return _Reach.ALIAS
    return None


def _source(config_dir: Path, path: str) -> _Source:
    """Locate *path*, the file a rename moves, for :func:`_reach`. Blocking."""
    try:
        located = _locate(config_dir, path, path)
    except OSError:
        located = _Located(path, path)
    return _Source(config_dir, path, located)


def _locate(config_dir: Path, rel: str, lexical: str) -> _Located:
    """
    Resolve config-relative *rel* through symlinks; ``None`` parts resolve outside. Blocking.

    ``entry`` is the folder resolved plus *rel*'s own name, where a rename finds it;
    ``file`` follows a symlinked file too. A missing folder holds no symlink, so *rel*
    is taken as its normalised form *lexical*. Raises ``OSError`` for a folder that
    can't be resolved (a symlink loop, no permission).
    """
    root = config_dir.resolve()
    path = config_dir / rel
    try:
        folder = path.parent.resolve(strict=True)
    except FileNotFoundError:
        return _Located(lexical, lexical)
    return _Located(_relative(folder / path.name, root), _relative(path.resolve(), root))


def _relative(path: Path, root: Path) -> str | None:
    return path.relative_to(root).as_posix() if path.is_relative_to(root) else None


def _same(rel: str | None, other: str | None) -> bool:
    return rel is not None and other is not None and dependency_key(rel) == dependency_key(other)


def _is_alias(source: _Source, rel: str) -> bool:
    """Whether candidate *rel* is a symlink of another name onto *source*'s file. Blocking."""
    # The walk found *rel*, so its folder resolves.
    located = _locate(source.config_dir, rel, rel)
    return _same(located.file, source.located.file) and not _same(
        located.entry, source.located.entry
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
    return _Target(None, None)  # pragma: no cover - the parser rejects an unclosed mapping


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
    ref = dependency_key(ref)  # Windows separators, before the path is normalized
    if "$" in ref or ref.startswith("/") or re.match(r"[A-Za-z]:", ref):
        return None
    target = posixpath.normpath(posixpath.join(folder, ref))
    return None if target == ".." or target.startswith("../") else target


def _read(path: Path) -> str | None:
    """Return the text of *path*; ``""`` if it is gone, ``None`` if it can't be read."""
    try:
        return path.read_bytes().decode("utf-8")  # keeps CRLF
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError) as err:
        _LOGGER.warning("Can't check %s for includes: %s", path, err)
        return None


def _check_only(config_dir: Path, rel: str) -> bool:
    """
    Whether candidate *rel* is only checked, never rewritten. Blocking.

    A secrets file (``secrets.<x>.yaml`` too) would need the secrets lock and must stay
    out of version history. A symlink, or a file reached through one, would be replaced
    by a regular file holding its target's text, so it is left as it is too.
    """
    return is_secrets_like_name(rel) or resolved_rel(config_dir, rel) != rel


def _candidates(config_dir: Path) -> Iterable[tuple[str, str]]:
    """Each scanned YAML, with the folder its includes resolve from."""
    # Devices, packages and secrets files at any depth (``!secret`` in a folder reads
    # that folder's own secrets.yaml), and hidden files: no device or package, but
    # devices can include them.
    yield from ((rel, posixpath.dirname(rel)) for rel in iter_referrer_yamls(config_dir))
    # Unarchiving moves a device back to the top level, so its includes resolve from there.
    yield from ((rel, "") for rel in list_archived_devices(config_dir))
