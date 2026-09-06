"""Read / rewrite the ``encryption.key`` literal of the esphome OTA platform item."""

from __future__ import annotations

import re

from .scalar import ESPHOME_YAML_INDENT, _quote, _split_value_and_comment, _strip_yaml_quotes
from .scan import child_block_end, is_list_item_line, key_line_res, top_list_item_starts
from .top_block import _locate_top_block

_PLATFORM_RE = re.compile(r"^\s*(?:-\s+)?platform:\s*(?P<value>[^\s#]+)")
_INDIRECTION_PREFIXES = ("!secret", "${")


def read_ota_encryption_key(yaml_text: str) -> str | None:
    """
    Return the raw ``encryption: key:`` value of the esphome OTA item, or ``None``.

    ``None`` covers no ``ota:`` block, no esphome platform item, no
    ``encryption:`` block, and a bare ``encryption:`` (which inherits the
    api key). Quotes stay intact, like :func:`read_yaml_scalar`.
    """
    lines = yaml_text.splitlines(keepends=True)
    located = _locate_key_line(lines)
    if located is None:
        return None
    value, _comment = _split_value_and_comment(_rest_after_key(lines[located]))
    return value.strip()


def rewrite_ota_encryption_key(yaml_text: str, new_key: str) -> str:
    """
    Replace a literal ``encryption: key:`` under the esphome OTA item with *new_key*.

    An indirected value (``!secret`` / ``${…}``), a bare ``encryption:``
    or a missing item leaves the text unchanged, matching
    :func:`rewrite_api_encryption_key`.
    """
    lines = yaml_text.splitlines(keepends=True)
    located = _locate_key_line(lines)
    if located is None:
        return yaml_text
    line = lines[located]
    body = line.rstrip("\n\r")
    value, comment = _split_value_and_comment(_rest_after_key(body))
    if _strip_yaml_quotes(value.strip()).startswith(_INDIRECTION_PREFIXES):
        return yaml_text
    head = body[: len(body) - len(_rest_after_key(body))]
    lines[located] = f"{head} {_quote(new_key)}{comment}{line[len(body) :]}"
    return "".join(lines)


def _locate_ota_esphome_item(lines: list[str]) -> tuple[int, int, str] | None:
    """
    Locate the esphome platform item under ``ota:`` as ``(start, end, child_indent)``.

    Handles the list form (``- platform: esphome``) and the legacy bare
    mapping, where a missing ``platform:`` also means esphome.
    """
    located = _locate_top_block(lines, "ota")
    if located is None:
        return None
    block_start, block_end, indent = located
    item_starts = top_list_item_starts(lines, block_start, block_end)
    if not item_starts:
        return (
            (block_start, block_end, indent)
            if _item_platform(lines, block_start + 1, block_end, indent) in (None, "esphome")
            else None
        )
    for idx, start in enumerate(item_starts):
        end = item_starts[idx + 1] if idx + 1 < len(item_starts) else block_end
        dash_indent = _leading(lines[start])
        child_indent = _item_child_indent(lines, start, end, dash_indent)
        if _item_platform(lines, start, end, child_indent) == "esphome":
            return start, end, child_indent
    return None


def _item_platform(lines: list[str], start: int, end: int, child_indent: str) -> str | None:
    """Return the item's own ``platform:`` value (dash line included), or ``None``."""
    for idx in range(start, end):
        body = lines[idx].rstrip("\n\r")
        stripped = body.lstrip(" ")
        if not stripped or stripped.startswith("#"):
            continue
        leading = body[: len(body) - len(stripped)]
        on_dash = is_list_item_line(stripped) and idx == start
        if leading != child_indent and not on_dash:
            continue
        match = _PLATFORM_RE.match(body)
        if match is not None:
            return match.group("value").strip("\"'")
    return None


def _item_child_indent(lines: list[str], start: int, end: int, dash_indent: str) -> str:
    """Indent of the item's mapping keys: the dash column plus the ``- `` width."""
    dash_line = lines[start].rstrip("\n\r")
    stripped = dash_line.lstrip(" ")
    if stripped.rstrip(" ") != "-":
        return dash_indent + " " * (len(stripped) - len(stripped[1:].lstrip(" ")))
    for idx in range(start + 1, end):
        body = lines[idx].rstrip("\n\r")
        if body.strip() and not body.lstrip().startswith("#"):
            return _leading(body)
    return dash_indent + ESPHOME_YAML_INDENT


def _locate_key_line(lines: list[str]) -> int | None:
    """Index of the ``key:`` scalar line inside the item's ``encryption:`` block."""
    item = _locate_ota_esphome_item(lines)
    if item is None:
        return None
    start, end, child_indent = item
    header_re, _scalar_re = key_line_res("encryption", prefix=f"^{re.escape(child_indent)}")
    dash_header_re = re.compile(rf"^{re.escape(_leading(lines[start]))}-\s+encryption:\s*(?:#.*)?$")
    for idx in range(start, end):
        body = lines[idx].rstrip("\n\r")
        if header_re.match(body) or (idx == start and dash_header_re.match(body)):
            block_end = child_block_end(lines, idx, end, child_indent)
            _key_header_re, key_scalar_re = key_line_res("key", prefix=r"^\s+")
            for key_idx in range(idx + 1, block_end):
                key_body = lines[key_idx].rstrip("\n\r")
                if len(_leading(key_body)) > len(child_indent) and key_scalar_re.match(key_body):
                    return key_idx
            return None
    return None


def _rest_after_key(body: str) -> str:
    """Everything after ``key:`` on a ``key:`` scalar line."""
    return body.split("key:", 1)[1]


def _leading(line: str) -> str:
    """Leading spaces of *line*."""
    return line[: len(line) - len(line.lstrip(" "))]
