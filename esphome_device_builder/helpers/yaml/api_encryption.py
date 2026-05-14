"""Generate / rewrite the ESPHome ``api.encryption.key`` literal."""

from __future__ import annotations

import base64
import secrets

from .scalar import _quote, _strip_yaml_quotes, rewrite_yaml_scalar


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
        if unquoted.startswith("!secret") or unquoted.startswith("${"):
            return None
        return rendered

    return rewrite_yaml_scalar(yaml_text, ("api", "encryption", "key"), _swap)
