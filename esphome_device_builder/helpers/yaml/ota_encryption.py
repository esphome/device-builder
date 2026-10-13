"""Read / rewrite the ``encryption.key`` literal of the esphome OTA platform item."""

from __future__ import annotations

import re
from collections.abc import Callable

from .inline import _instance_bounds, _key_block_frame, _locate_handler_range
from .scalar import (
    YamlUpsertNotSupportedError,
    _quote,
    _split_value_and_comment,
    _strip_yaml_quotes,
    block_body_is_list,
    read_yaml_scalar,
    rewrite_yaml_scalar,
)
from .scan import leading_ws, normalize_trailing_newline, top_list_item_starts
from .top_block import _locate_top_block

_KEY_PATH = ("encryption", "key")
_OLD_KEY_PATH = ("encryption", "old_key")


def read_ota_encryption_key(yaml_text: str) -> str | None:
    """Raw ``encryption: key:`` of the esphome OTA item; ``None`` when absent or unreadable."""
    return _read_encryption_scalar(yaml_text, _KEY_PATH)


def read_ota_encryption_old_key(yaml_text: str) -> str | None:
    """Raw ``encryption: old_key:`` of the esphome OTA item; ``None`` when absent or unreadable."""
    return _read_encryption_scalar(yaml_text, _OLD_KEY_PATH)


def read_ota_encryption_keys(yaml_text: str) -> tuple[str | None, str | None]:
    """Raw ``(key, old_key)`` of the esphome OTA item's ``encryption:`` block from one walk."""
    lines = yaml_text.splitlines(keepends=True)
    block = _locate_encryption_block(lines)
    if block is None:
        return None, None
    text = "".join(lines[block[0] : block[1]])
    return read_yaml_scalar(text, _KEY_PATH), read_yaml_scalar(text, _OLD_KEY_PATH)


def rewrite_ota_encryption_key(yaml_text: str, transform: Callable[[str], str | None]) -> str:
    """Rewrite the esphome OTA item's ``encryption: key:`` scalar through *transform*."""
    lines = yaml_text.splitlines(keepends=True)
    block = _locate_encryption_block(lines)
    if block is None:
        return yaml_text
    start, end = block
    rewritten = rewrite_yaml_scalar("".join(lines[start:end]), _KEY_PATH, transform)
    return "".join([*lines[:start], rewritten, *lines[end:]])


def set_ota_encryption_old_key(yaml_text: str, old_key: str) -> str:
    """Write ``old_key:`` under the item's ``encryption:``, replacing any existing line."""
    return _splice_encryption_child(yaml_text, _OLD_KEY_PATH[-1], old_key)


def drop_ota_encryption_key(yaml_text: str) -> str:
    """Drop the ``key:`` lines under the item's ``encryption:``; a multi-line value raises."""
    return _splice_encryption_child(yaml_text, _KEY_PATH[-1], None)


def drop_ota_encryption_old_key(yaml_text: str) -> str:
    """Drop the ``old_key:`` lines under the item's ``encryption:``; a multi-line value raises."""
    return _splice_encryption_child(yaml_text, _OLD_KEY_PATH[-1], None)


def _has_encryption_block(yaml_text: str) -> bool:
    """Whether the esphome OTA item has an ``encryption:`` block the line walker can edit."""
    return _locate_encryption_block(yaml_text.splitlines(keepends=True)) is not None


def _read_encryption_scalar(yaml_text: str, path: tuple[str, str]) -> str | None:
    """Raw scalar at *path* inside the esphome OTA item's ``encryption:`` block."""
    lines = yaml_text.splitlines(keepends=True)
    block = _locate_encryption_block(lines)
    if block is None:
        return None
    start, end = block
    return read_yaml_scalar("".join(lines[start:end]), path)


def _splice_encryption_child(yaml_text: str, child: str, value: str | None) -> str:
    """Drop the item's ``encryption: <child>:`` lines, appending ``child: value`` when given."""
    text, nl = normalize_trailing_newline(yaml_text) if value is not None else (yaml_text, "")
    lines = text.splitlines(keepends=True)
    block = _locate_encryption_block(lines)
    if block is None:
        return yaml_text
    start, end = block
    kept = _drop_encryption_child(lines, start, end, child)
    if value is not None:
        indent = _key_block_frame(lines, start, end, leading_ws(lines[start])).child_indent
        kept.append(f"{indent}{child}: {_quote(value)}{nl}")
    return "".join([*lines[: start + 1], *kept, *lines[end:]])


def _drop_encryption_child(lines: list[str], start: int, end: int, child: str) -> list[str]:
    """Body lines of the block minus every ``<child>:`` line; a multi-line value raises."""
    child_re = re.compile(rf"^\s+{re.escape(child)}:(?:\s|$)")
    kept: list[str] = []
    for idx in range(start + 1, end):
        body = lines[idx].rstrip("\n\r")
        if not child_re.match(body):
            kept.append(lines[idx])
            continue
        if _continues_on_next_line(lines, idx, end):
            raise YamlUpsertNotSupportedError(
                f"the OTA encryption {child} spans more than one line; the line-based "
                "rewrite can't safely drop it."
            )
    return kept


def _continues_on_next_line(lines: list[str], key_idx: int, end: int) -> bool:
    """Whether the ``key:`` at *key_idx* is a block scalar or continues on a deeper line."""
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
    """Span of the item's ``encryption:`` block; ``None`` when absent or unreadable."""
    try:
        item = _locate_ota_esphome_item(lines)
    except YamlUpsertNotSupportedError:
        return None
    return None if item is None else _locate_handler_range(lines, item, "encryption")


def _locate_ota_esphome_item(lines: list[str]) -> tuple[int, int, str] | None:
    """Locate the esphome platform item under ``ota:`` as ``(start, end, child_indent)``."""
    located = _locate_top_block(lines, "ota")
    if located is None:
        return None
    block_start, block_end, _indent = located
    # A mapping body can hold an action list; only a leading dash makes a list.
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
