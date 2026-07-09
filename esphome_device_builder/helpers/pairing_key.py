"""One-shot pairing key for the ``--remote-build-only`` first-pair bootstrap."""

from __future__ import annotations

import hmac
import re
import secrets

# No 0/O, 1/I/L, or U (transcription ambiguity); 30**16 ≈ 2**78.
_KEY_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"
_KEY_CHARS = 16
_KEY_GROUP = 4

# Bounds normalize cost on a wire-supplied key the offloader-side
# validator doesn't gate (a peer bypassing the WS command). Far above
# any real grouped key.
_MAX_PRESENTED_CHARS = 256

_NON_ALNUM = re.compile(r"[^0-9A-Z]")


def generate_pairing_key() -> str:
    """Return a one-shot pairing key, grouped ``XXXX-XXXX-XXXX-XXXX``."""
    chars = "".join(secrets.choice(_KEY_ALPHABET) for _ in range(_KEY_CHARS))
    return "-".join(chars[i : i + _KEY_GROUP] for i in range(0, _KEY_CHARS, _KEY_GROUP))


def pairing_key_matches(expected: str, presented: str | None) -> bool:
    """
    Constant-time comparison of a presented pairing key against *expected*.

    Separators, whitespace, and case are ignored. The key is a real
    secret (unlike the pin), hence ``hmac.compare_digest``.
    """
    if not presented or len(presented) > _MAX_PRESENTED_CHARS:
        return False
    return hmac.compare_digest(_normalize(expected).encode(), _normalize(presented).encode())


def _normalize(value: str) -> str:
    """Uppercase and strip everything that isn't ``[0-9A-Z]``."""
    return _NON_ALNUM.sub("", value.upper())
