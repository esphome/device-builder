"""Read / rewrite the ``encryption.key`` literal of the esphome OTA platform item."""

from __future__ import annotations

import re
from collections.abc import Callable

from .inline import _instance_bounds, _locate_handler_range
from .scalar import (
    YamlUpsertNotSupportedError,
    _split_value_and_comment,
    _strip_yaml_quotes,
    block_body_is_list,
    read_yaml_scalar,
    rewrite_yaml_scalar,
)
from .scan import leading_ws, top_list_item_starts
from .top_block import _locate_top_block

_KEY_PATH = ("encryption", "key")


def read_ota_encryption_key(yaml_text: str) -> str | None:
    """
    Return the raw ``encryption: key:`` value of the esphome OTA item, or ``None``.

    ``None`` covers no ``ota:`` block, an ``ota:`` header the line walker
    can't read (``!include``, flow style), no esphome platform item, no
    ``encryption:`` block, and a bare ``encryption:`` (which inherits the
    api key). Quotes stay intact.
    """
    lines = yaml_text.splitlines(keepends=True)
    block = _locate_encryption_block(lines)
    if block is None:
        return None
    start, end = block
    return read_yaml_scalar("".join(lines[start:end]), _KEY_PATH)


def rewrite_ota_encryption_key(yaml_text: str, transform: Callable[[str], str | None]) -> str:
    """
    Rewrite the esphome OTA item's ``encryption: key:`` scalar through *transform*.

    *transform* follows :func:`rewrite_yaml_scalar`; a missing item, block
    or key leaves the text unchanged.
    """
    lines = yaml_text.splitlines(keepends=True)
    block = _locate_encryption_block(lines)
    if block is None:
        return yaml_text
    start, end = block
    rewritten = rewrite_yaml_scalar("".join(lines[start:end]), _KEY_PATH, transform)
    return "".join([*lines[:start], rewritten, *lines[end:]])


def drop_ota_encryption_key(yaml_text: str) -> str:
    """
    Remove every ``key:`` line from the esphome OTA item's ``encryption:`` block.

    The bare block that remains inherits the api key. A missing item, block
    or key leaves the text unchanged. A key whose value continues on the
    next line (a block scalar, a value on its own line) raises
    :class:`YamlUpsertNotSupportedError`, since dropping the line alone
    would reshape the block.
    """
    lines = yaml_text.splitlines(keepends=True)
    block = _locate_encryption_block(lines)
    if block is None:
        return yaml_text
    start, end = block
    key_re = re.compile(r"^\s+key:(?:\s|$)")
    kept: list[str] = []
    for idx in range(start + 1, end):
        body = lines[idx].rstrip("\n\r")
        if not key_re.match(body):
            kept.append(lines[idx])
            continue
        if _continues_on_next_line(lines, idx, end):
            raise YamlUpsertNotSupportedError(
                "the OTA encryption key spans more than one line; the line-based "
                "rewrite can't safely drop it."
            )
    return "".join([*lines[: start + 1], *kept, *lines[end:]])


def _continues_on_next_line(lines: list[str], key_idx: int, end: int) -> bool:
    """Whether the ``key:`` at *key_idx* has a block-scalar value or a value on a deeper line."""
    value, _comment = _split_value_and_comment(lines[key_idx].rstrip("\n\r").split(":", 1)[1])
    if value.strip().startswith(("|", ">")):
        return True
    key_indent = len(leading_ws(lines[key_idx]))
    for idx in range(key_idx + 1, end):
        body = lines[idx].rstrip("\n\r")
        if not body.strip() or body.lstrip().startswith("#"):
            continue
        return len(leading_ws(body)) > key_indent
    return False


def _locate_encryption_block(lines: list[str]) -> tuple[int, int] | None:
    """
    Line span of the esphome OTA item's ``encryption:`` block, or ``None``.

    An ``ota:`` header the walker can't read (``!include``, flow style)
    reads as ``None`` too; a mismatched pair behind it is left to esphome's
    validation, which the push path runs before persisting.
    """
    try:
        item = _locate_ota_esphome_item(lines)
    except YamlUpsertNotSupportedError:
        return None
    return None if item is None else _locate_handler_range(lines, item, "encryption")


def _locate_ota_esphome_item(lines: list[str]) -> tuple[int, int, str] | None:
    """
    Locate the esphome platform item under ``ota:`` as ``(start, end, child_indent)``.

    Handles the list form (``- platform: esphome``) and the single-mapping
    form (``platform: esphome`` directly under ``ota:``).
    """
    located = _locate_top_block(lines, "ota")
    if located is None:
        return None
    block_start, block_end, _indent = located
    # A mapping form can hold an action list; only a leading dash makes a list.
    if not block_body_is_list(lines, block_start, block_end):
        return located if _item_platform(lines, *located) == "esphome" else None
    item_starts = top_list_item_starts(lines, block_start, block_end)
    for span in _instance_bounds(lines, item_starts, block_end):
        if _item_platform(lines, *span) == "esphome":
            return span
    return None


def _item_platform(lines: list[str], start: int, end: int, child_indent: str) -> str | None:
    """Return the item's own ``platform:`` value (dash line included), or ``None``."""
    dash = re.escape(leading_ws(lines[start])) + r"-\s+"
    platform_re = re.compile(rf"^(?:{re.escape(child_indent)}|{dash})platform:(?P<rest>.*)$")
    for idx in range(start, end):
        match = platform_re.match(lines[idx].rstrip("\n\r"))
        if match is not None:
            value, _comment = _split_value_and_comment(match.group("rest"))
            return _strip_yaml_quotes(value)
    return None
