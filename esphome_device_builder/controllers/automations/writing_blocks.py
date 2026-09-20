"""Top-level list blocks in every shape esphome accepts: mapping, flow or list."""

from __future__ import annotations

import re
from collections.abc import Callable
from functools import wraps
from typing import Any, Concatenate

from ruamel.yaml import YAMLError
from ruamel.yaml.comments import CommentedMap, CommentedSeq, TaggedScalar
from ruamel.yaml.composer import ComposerError
from ruamel.yaml.events import (
    AliasEvent,
    CollectionEndEvent,
    CollectionStartEvent,
    Event,
    NodeEvent,
    ScalarEvent,
)

from ...helpers.api import CommandError
from ...helpers.yaml import _normalize_multi_conf_block
from ...helpers.yaml.scan import find_block_header, key_line_res
from ...helpers.yaml.writing_layout import _build_diff_for_append
from ...models.api import ErrorCode
from ...models.automations import YamlDiff
from .emitter import dump
from .parsing import make_yaml


def in_list_form[**P](
    op: Callable[Concatenate[str, str, P], tuple[str, YamlDiff]],
) -> Callable[Concatenate[str, str, P], tuple[str, YamlDiff]]:
    """Run *op* with a mapping-form ``<domain>:`` block first rewritten as a one-item list."""

    @wraps(op)
    def run(yaml_text: str, domain: str, *args: P.args, **kwargs: P.kwargs) -> tuple[str, YamlDiff]:
        expanded = _expand_flow_block(yaml_text, domain)
        listed = _normalize_multi_conf_block(expanded, domain) or expanded
        if listed != yaml_text:
            _require_unaliased(yaml_text, domain, nested=expanded != yaml_text)
        _require_block_style(listed, domain)
        new_text, diff = op(listed, domain, *args, **kwargs)
        if listed == yaml_text:
            return new_text, diff
        if not yaml_text.endswith("\n") and new_text.endswith("\n"):
            # The editor's splice cannot add a final newline the file never had.
            new_text = new_text[:-1]
        return new_text, _build_diff_for_append(yaml_text, new_text)

    return run


def _expand_flow_block(yaml_text: str, domain: str) -> str:
    """Rewrite a one-line flow-style ``<domain>: {...}`` / ``[...]`` as a block-form list."""
    lines = yaml_text.splitlines(keepends=True)
    idx = _inline_header_index(lines, domain)
    if idx is None:
        return yaml_text
    try:
        loaded = make_yaml().load(lines[idx])
    except YAMLError:
        return yaml_text  # a flow value spanning lines is left for _require_block_style
    block = loaded.get(domain) if isinstance(loaded, dict) else None
    if not isinstance(block, (dict, list)):
        return yaml_text
    comment = _pop_trailing_comment(loaded, domain)
    items = block if isinstance(block, list) else [block]
    _set_block_style(items)
    body = dump(items) if items else ""
    header_line = f"{domain}:" + (f"  {comment}" if comment else "") + "\n"
    return "".join(lines[:idx]) + header_line + body + "".join(lines[idx + 1 :])


def _pop_trailing_comment(loaded: CommentedMap, key: str) -> str:
    """Detach and return the end-of-line comment ruamel kept on ``loaded[key]``, or ``""``."""
    tokens = loaded.ca.items.pop(key, None) or []
    found = [
        token.value.strip()
        for slot in tokens
        for token in (slot if isinstance(slot, list) else [slot])
        if token is not None and getattr(token, "value", "").strip()
    ]
    block = loaded[key]
    for node in (block, *(block if isinstance(block, list) else [])):
        if hasattr(node, "ca"):
            node.ca.items.clear()
            node.ca.comment = None
    return found[0] if found else ""


def _inline_header_index(lines: list[str], domain: str) -> int | None:
    """Index of a column-0 ``<domain>: <value>`` line, or None."""
    inline_re = key_line_res(domain, prefix="^")[1]
    return next((i for i, line in enumerate(lines) if inline_re.match(line.rstrip("\n\r"))), None)


def _set_block_style(node: Any) -> None:
    """Clear ruamel's flow-style flags on *node* and everything under it."""
    if isinstance(node, (CommentedMap, CommentedSeq)):
        node.fa.set_block_style()
    children = node.values() if isinstance(node, dict) else node if isinstance(node, list) else ()
    for child in children:
        _set_block_style(child)


def _require_unaliased(yaml_text: str, domain: str, *, nested: bool) -> None:
    """Refuse to rewrite a block whose anchor (any anchor under it when *nested*) is aliased."""
    events = list(make_yaml().parse(yaml_text))
    anchors = _block_anchors(events, domain)
    if not nested:
        anchors = anchors[:1]
    aliased = {event.anchor for event in events if isinstance(event, AliasEvent)}
    hit = next((name for name in anchors if name in aliased), None)
    if hit is None:
        return
    if hit == anchors[0]:
        msg = f"{domain}: is anchored as &{hit} and aliased; rewrite it as a list first"
    else:
        msg = f"{domain}: holds an aliased anchor &{hit}; rewrite it as a list first"
    raise CommandError(ErrorCode.INVALID_ARGS, msg)


def _block_anchors(events: list[Event], domain: str) -> list[str | None]:
    """Anchors declared under the top-level ``<domain>:`` value, the value's own first."""
    depth = 0
    node = -1
    key_node: int | None = None
    anchors: list[str | None] = []
    for event in events:
        if isinstance(event, CollectionEndEvent):
            depth -= 1
            continue
        if depth == 1 and isinstance(event, NodeEvent):
            node += 1
            is_key = node % 2 == 0 and isinstance(event, ScalarEvent) and event.value == domain
            if key_node is None and is_key:
                key_node = node
        in_value = key_node is not None and node == key_node + 1
        if in_value and isinstance(event, NodeEvent) and not isinstance(event, AliasEvent):
            anchors.append(event.anchor)
        if isinstance(event, CollectionStartEvent):
            depth += 1
    return anchors


def _require_block_style(yaml_text: str, domain: str) -> None:
    """Refuse a ``<domain>:`` value the line splicers cannot see, naming what it is."""
    lines = yaml_text.splitlines()
    idx = _inline_header_index(lines, domain)
    if idx is None or find_block_header(lines, domain) is not None:
        return
    try:
        value = make_yaml().load(lines[idx])[domain]
    except ComposerError:
        msg = (
            f"{domain}: is an alias; rewrite it as a block first"
            if re.match(rf"{re.escape(domain)}:\s*\*", lines[idx])
            else f"{domain}: holds an alias inside a flow value; rewrite it as a block first"
        )
    except YAMLError:
        msg = (
            f"{domain}: is written in flow style across several lines; rewrite it as a block first"
        )
    else:
        msg = (
            f"{domain}: is provided by a tag; edit the included file instead"
            if isinstance(value, TaggedScalar)
            else f"{domain}: holds a scalar, not an automation block"
        )
    raise CommandError(ErrorCode.INVALID_ARGS, msg)
