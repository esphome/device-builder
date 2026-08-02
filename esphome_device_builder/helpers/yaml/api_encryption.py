"""Generate / rewrite the ESPHome ``api.encryption.key`` literal."""

from __future__ import annotations

import base64
import secrets

from .scalar import (
    ESPHOME_YAML_INDENT,
    YamlUpsertNotSupportedError,
    _quote,
    _strip_yaml_quotes,
    read_yaml_scalar,
    rewrite_yaml_scalar,
)
from .top_block import _find_prepend_anchor, _locate_top_block

_API_ENCRYPTION_KEY_PATH = ("api", "encryption", "key")


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

    return rewrite_yaml_scalar(yaml_text, _API_ENCRYPTION_KEY_PATH, _swap)


def upsert_api_encryption_key(yaml_text: str, new_key: str) -> str:
    """
    Set ``api.encryption.key`` to *new_key*, inserting missing structure.

    Rewrites an existing literal in place; an indirected key
    (``!secret`` / ``${…}``) returns the text unchanged. Raises
    :class:`YamlUpsertNotSupportedError` for shapes the line-based
    walker can't safely edit.
    """
    if read_yaml_scalar(yaml_text, _API_ENCRYPTION_KEY_PATH) is not None:
        return rewrite_api_encryption_key(yaml_text, new_key)

    rendered = _quote(new_key)
    nl = "\r\n" if "\r\n" in yaml_text else "\n"
    lines = yaml_text.splitlines(keepends=True)
    located = _locate_top_block(lines, "api")

    if located is None:
        anchor = _find_prepend_anchor(lines)
        prefix = "".join(lines[:anchor])
        rest = "".join(lines[anchor:])
        sep = "" if not rest or rest.startswith(("\n", "\r\n")) else nl
        block = (
            f"api:{nl}{ESPHOME_YAML_INDENT}encryption:{nl}"
            f"{ESPHOME_YAML_INDENT * 2}key: {rendered}{nl}{sep}"
        )
        return f"{prefix}{block}{rest}"

    block_start, block_end, indent = located
    enc_idx = _find_encryption_header(lines, block_start, block_end, indent)
    if enc_idx is not None:
        new_line = f"{indent}{ESPHOME_YAML_INDENT}key: {rendered}{nl}"
        return "".join([*lines[: enc_idx + 1], new_line, *lines[enc_idx + 1 :]])

    # Trim trailing blank lines so the insert lands right after the
    # block's last content line, not after the visual gap.
    insert_at = block_end
    while insert_at > block_start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    new_lines = [
        f"{indent}encryption:{nl}",
        f"{indent}{ESPHOME_YAML_INDENT}key: {rendered}{nl}",
    ]
    return "".join([*lines[:insert_at], *new_lines, *lines[insert_at:]])


def _find_encryption_header(
    lines: list[str], block_start: int, block_end: int, indent: str
) -> int | None:
    """Find the ``encryption:`` header at *indent* inside the ``api:`` block span."""
    for i in range(block_start + 1, block_end):
        body = lines[i].rstrip("\n\r")
        stripped = body.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        if body[: len(body) - len(stripped)] != indent:
            continue
        if not stripped.startswith("encryption:"):
            continue
        if stripped[len("encryption:") :].split("#", 1)[0].strip():
            raise YamlUpsertNotSupportedError(
                "api.encryption uses an inline value or flow-style mapping; "
                "the line-based upsert can't safely edit it."
            )
        return i
    return None
