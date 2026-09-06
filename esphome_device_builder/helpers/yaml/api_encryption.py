"""Generate / rewrite the ESPHome ``api.encryption.key`` literal."""

from __future__ import annotations

import base64
import re
import secrets
from collections.abc import Callable

from .ota_encryption import read_ota_encryption_key, rewrite_ota_encryption_key
from .scalar import (
    ESPHOME_YAML_INDENT,
    YamlUpsertNotSupportedError,
    _quote,
    _strip_yaml_quotes,
    is_plain_literal_scalar,
    read_yaml_scalar,
    rewrite_yaml_scalar,
)
from .scan import key_line_res, normalize_trailing_newline, trim_trailing_blanks
from .top_block import _locate_top_block, _prepend_top_block

API_ENCRYPTION_KEY_PATH = ("api", "encryption", "key")


def literal_key_matches(raw: str | None, key: str) -> bool:
    """Whether the raw YAML scalar *raw* is the literal *key*."""
    return raw is not None and _strip_yaml_quotes(raw) == key


def generate_api_encryption_key() -> str:
    """Return a fresh 32-byte ESPHome API encryption key, base64-encoded."""
    return base64.b64encode(secrets.token_bytes(32)).decode()


def rewrite_api_encryption_key(yaml_text: str, new_key: str) -> str:
    """
    Replace the literal ``key:`` under ``api: -> encryption:`` with *new_key*.

    An indirected key (``!secret`` / ``${…}``) leaves the text unchanged. An
    explicit esphome OTA ``encryption: key:`` literal follows the api key;
    an indirected one raises :class:`YamlUpsertNotSupportedError`.
    """
    rewritten = rewrite_yaml_scalar(yaml_text, API_ENCRYPTION_KEY_PATH, _literal_swap(new_key))
    return _follow_ota_key(rewritten, new_key, follow=True)


def upsert_api_encryption_key(yaml_text: str, new_key: str) -> str:
    """
    Set ``api.encryption.key`` to *new_key*, inserting missing structure.

    Rewrites an existing literal like :func:`rewrite_api_encryption_key`.
    Inserting a key next to an explicit esphome OTA key that differs raises
    :class:`YamlUpsertNotSupportedError`, as do shapes the line-based
    walker can't safely edit.
    """
    if read_yaml_scalar(yaml_text, API_ENCRYPTION_KEY_PATH) is not None:
        return rewrite_api_encryption_key(yaml_text, new_key)
    return _follow_ota_key(_insert_api_encryption_key(yaml_text, new_key), new_key, follow=False)


def _insert_api_encryption_key(yaml_text: str, new_key: str) -> str:
    """Insert ``api.encryption.key`` where the ``api:`` block has no ``key:`` yet."""
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


def _literal_swap(new_key: str) -> Callable[[str], str | None]:
    """Transform replacing a literal ``key:`` with *new_key*; ``!secret`` / ``${…}`` stay."""
    rendered = _quote(new_key)

    def _swap(raw: str) -> str | None:
        # An empty ``key:`` is fillable; only a tagged or substituted value is kept.
        if not _strip_yaml_quotes(raw) or is_plain_literal_scalar(raw):
            return rendered
        return None

    return _swap


def _follow_ota_key(yaml_text: str, new_key: str, *, follow: bool) -> str:
    """
    Reconcile an explicit esphome OTA key with a literal api key of *new_key*.

    With *follow* the OTA literal is rewritten to match; without it a
    differing OTA key is refused, since the device requires that key.
    """
    if not literal_key_matches(read_yaml_scalar(yaml_text, API_ENCRYPTION_KEY_PATH), new_key):
        return yaml_text
    ota_key = read_ota_encryption_key(yaml_text)
    if ota_key is None or literal_key_matches(ota_key, new_key):
        return yaml_text
    if not follow:
        if not is_plain_literal_scalar(ota_key):
            raise YamlUpsertNotSupportedError(
                "the OTA platform's own encryption key is provided via !secret or a "
                "substitution, so it cannot be checked against the pushed key; the api "
                "key was left provisioned at runtime"
            )
        raise YamlUpsertNotSupportedError(
            "the config gives the OTA platform its own encryption key, which the "
            "device requires; the api key was left provisioned at runtime"
        )
    rewritten = rewrite_ota_encryption_key(yaml_text, _literal_swap(new_key))
    if rewritten == yaml_text:
        raise YamlUpsertNotSupportedError(
            "the OTA encryption key is provided via !secret or a substitution "
            "and must match the api encryption key"
        )
    return rewritten


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
