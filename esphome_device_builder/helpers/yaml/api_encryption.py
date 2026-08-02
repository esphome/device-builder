"""Generate / rewrite the ESPHome ``api.encryption.key`` literal."""

from __future__ import annotations

import base64
import re
import secrets

from .scalar import (
    ESPHOME_YAML_INDENT,
    YamlUpsertNotSupportedError,
    _quote,
    _strip_yaml_quotes,
    read_yaml_scalar,
    rewrite_yaml_scalar,
)
from .scan import key_line_res, normalize_trailing_newline, trim_trailing_blanks
from .top_block import _locate_top_block, _prepend_top_block

API_ENCRYPTION_KEY_PATH = ("api", "encryption", "key")


def generate_api_encryption_key() -> str:
    """Return a fresh 32-byte ESPHome API encryption key, base64-encoded."""
    return base64.b64encode(secrets.token_bytes(32)).decode()


def rewrite_api_encryption_key(yaml_text: str, new_key: str) -> str:
    """
    Replace the literal ``key:`` value under ``api: -> encryption:``.

    Used by the clone path so two devices forked from the same
    source don't share API encryption material — compromise of one
    device must not compromise its siblings. Only rewrites a
    *literal* key value; lines whose value is an indirection
    (``!secret …`` / ``${…}``) are left untouched, because the
    indirection target is shared on disk and stomping on the key
    here would silently desync the clone from whatever
    ``secrets.yaml`` / substitutions block actually drives the
    encryption. Returns the original text unchanged when no
    in-scope ``key:`` is found or when the value is an indirection.

    The replacement is rendered double-quoted so a base64 value
    that happens to start with a YAML special character
    (``!``/``%``/``@``/``-``/``?``/``&``/``*``) parses cleanly.
    """
    rendered = _quote(new_key)

    def _swap(raw: str) -> str | None:
        # Strip quotes before checking for indirection markers — both
        # ``key: !secret api_key`` and ``key: "${api_key}"`` are
        # valid YAML, and the second form's quotes would otherwise
        # mask the ``${`` prefix and cause us to rewrite a value the
        # user explicitly indirected.
        unquoted = _strip_yaml_quotes(raw)
        if unquoted.startswith(("!secret", "${")):
            return None
        return rendered

    return rewrite_yaml_scalar(yaml_text, API_ENCRYPTION_KEY_PATH, _swap)


def upsert_api_encryption_key(yaml_text: str, new_key: str) -> str:
    """
    Set ``api.encryption.key`` to *new_key*, inserting missing structure.

    Rewrites an existing literal in place; an indirected key
    (``!secret`` / ``${…}``) returns the text unchanged. Raises
    :class:`YamlUpsertNotSupportedError` for shapes the line-based
    walker can't safely edit.
    """
    if read_yaml_scalar(yaml_text, API_ENCRYPTION_KEY_PATH) is not None:
        return rewrite_api_encryption_key(yaml_text, new_key)

    rendered = _quote(new_key)
    yaml_text, nl = normalize_trailing_newline(yaml_text)
    lines = yaml_text.splitlines(keepends=True)
    located = _locate_top_block(lines, "api")

    if located is None:
        block = (
            f"api:{nl}{ESPHOME_YAML_INDENT}encryption:{nl}"
            f"{ESPHOME_YAML_INDENT * 2}key: {rendered}{nl}"
        )
        return _prepend_top_block(lines, block, nl)

    block_start, block_end, indent = located
    enc_idx = _find_encryption_header(lines, block_start, block_end, indent)
    if enc_idx is not None:
        new_line = f"{indent}{ESPHOME_YAML_INDENT}key: {rendered}{nl}"
        return "".join([*lines[: enc_idx + 1], new_line, *lines[enc_idx + 1 :]])

    insert_at = trim_trailing_blanks(lines, block_start, block_end)
    new_lines = [
        f"{indent}encryption:{nl}",
        f"{indent}{ESPHOME_YAML_INDENT}key: {rendered}{nl}",
    ]
    return "".join([*lines[:insert_at], *new_lines, *lines[insert_at:]])


def _find_encryption_header(
    lines: list[str], block_start: int, block_end: int, indent: str
) -> int | None:
    """Find the ``encryption:`` header at *indent* inside the ``api:`` block span."""
    header_re, scalar_re = key_line_res("encryption", prefix=f"^{re.escape(indent)}")
    for i in range(block_start + 1, block_end):
        content = lines[i].rstrip("\n\r")
        if header_re.match(content):
            return i
        if scalar_re.match(content):
            raise YamlUpsertNotSupportedError(
                "api.encryption uses an inline value or flow-style mapping; "
                "the line-based upsert can't safely edit it."
            )
    return None
