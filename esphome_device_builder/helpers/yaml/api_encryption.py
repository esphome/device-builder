"""Generate / rewrite the ESPHome ``api.encryption.key`` literal."""

from __future__ import annotations

import base64
import re
import secrets
from collections.abc import Callable

from .ota_encryption import (
    drop_ota_encryption_key,
    read_ota_encryption_key,
    rewrite_ota_encryption_key,
)
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


def ota_key_matches(yaml_text: str, key: str) -> bool:
    """Whether an explicit OTA key, if any, is *key*; an indirected one is left to esphome."""
    ota_key = read_ota_encryption_key(yaml_text)
    if ota_key is None or literal_key_matches(ota_key, key):
        return True
    # An empty key is fillable; only a tagged or substituted value is left alone.
    return bool(_strip_yaml_quotes(ota_key)) and not is_plain_literal_scalar(ota_key)


def generate_api_encryption_key() -> str:
    """Return a fresh 32-byte ESPHome API encryption key, base64-encoded."""
    return base64.b64encode(secrets.token_bytes(32)).decode()


def rewrite_api_encryption_key(yaml_text: str, new_key: str) -> str:
    """
    Replace the literal ``key:`` under ``api: -> encryption:`` with *new_key*.

    An indirected key (``!secret`` / ``${…}``) leaves the text unchanged. An
    explicit esphome OTA ``encryption: key:`` literal is dropped so the bare
    block inherits the api key; an indirected one raises
    :class:`YamlUpsertNotSupportedError`.
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


def rewrite_own_ota_encryption_key(yaml_text: str, new_key: str) -> str:
    """
    Replace the OTA platform's own literal key with *new_key* when there is no api key line.

    Any api key line means the OTA key must match it: a literal one is
    handled by :func:`rewrite_api_encryption_key`, an indirected one stays
    shared with the source together with the OTA key. An indirected or
    missing OTA key leaves the text unchanged.
    """
    if read_yaml_scalar(yaml_text, API_ENCRYPTION_KEY_PATH) is not None:
        return yaml_text
    return rewrite_ota_encryption_key(yaml_text, _literal_swap(new_key))


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

    An OTA key that would only duplicate the api key is dropped so the bare
    block inherits it. Without *follow* a differing OTA key is refused,
    since the device requires that key.
    """
    if not literal_key_matches(read_yaml_scalar(yaml_text, API_ENCRYPTION_KEY_PATH), new_key):
        return yaml_text
    ota_key = read_ota_encryption_key(yaml_text)
    if ota_key is None:
        return yaml_text
    if _strip_yaml_quotes(ota_key) and not is_plain_literal_scalar(ota_key):
        if not follow:
            raise YamlUpsertNotSupportedError(
                "the OTA platform's own encryption key is provided via !secret or a "
                "substitution, so it cannot be checked against the new api key."
            )
        raise YamlUpsertNotSupportedError(
            "the OTA encryption key is provided via !secret or a substitution "
            "and must match the api encryption key."
        )
    if not follow and _strip_yaml_quotes(ota_key) and not literal_key_matches(ota_key, new_key):
        raise YamlUpsertNotSupportedError(
            "the config gives the OTA platform its own encryption key, which the "
            "device requires, so no api key was written."
        )
    # Deliberately dropped, not rewritten to the same value: with a static api
    # key the firmware encrypts OTA with that key whatever the OTA block says,
    # so a second literal is only a copy to keep in sync, and the bare block is
    # the documented shape (esphome.io "ESPHome OTA Updates" > Encryption).
    return drop_ota_encryption_key(yaml_text)


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
