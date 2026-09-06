"""Read / rewrite the ``encryption.key`` literal of the esphome OTA platform item."""

from __future__ import annotations

import re
from collections.abc import Callable

from .inline import _instance_bounds, _locate_handler_range
from .scalar import (
    _split_value_and_comment,
    _strip_yaml_quotes,
    read_yaml_scalar,
    rewrite_yaml_scalar,
)
from .scan import leading_ws, top_list_item_starts
from .top_block import _locate_top_block

_KEY_PATH = ("encryption", "key")


def read_ota_encryption_key(yaml_text: str) -> str | None:
    """
    Return the raw ``encryption: key:`` value of the esphome OTA item, or ``None``.

    ``None`` covers no ``ota:`` block, no esphome platform item, no
    ``encryption:`` block, and a bare ``encryption:`` (which inherits the
    api key). Quotes stay intact, like :func:`read_yaml_scalar`.
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


def _locate_encryption_block(lines: list[str]) -> tuple[int, int] | None:
    """Line span of the esphome OTA item's ``encryption:`` block, or ``None``."""
    item = _locate_ota_esphome_item(lines)
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
    item_starts = top_list_item_starts(lines, block_start, block_end)
    if not item_starts:
        return located if _item_platform(lines, *located) == "esphome" else None
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
