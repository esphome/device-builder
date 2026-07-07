"""One-shot pairing key for the ``--remote-build-only`` first-pair bootstrap."""

from __future__ import annotations

import hmac
import re
import secrets

# No 0/O, 1/I/L, or U (transcription ambiguity); 30**16 ≈ 2**78.
_PSK_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"
_PSK_CHARS = 16
_PSK_GROUP = 4

_NON_ALNUM = re.compile(r"[^0-9A-Z]")


def generate_pairing_psk() -> str:
    """Return a one-shot pairing key, grouped ``XXXX-XXXX-XXXX-XXXX``."""
    chars = "".join(secrets.choice(_PSK_ALPHABET) for _ in range(_PSK_CHARS))
    return "-".join(chars[i : i + _PSK_GROUP] for i in range(0, _PSK_CHARS, _PSK_GROUP))


def pairing_psk_matches(expected: str, presented: str | None) -> bool:
    """
    Constant-time comparison of a presented pairing key against *expected*.

    Separators, whitespace, and case are ignored. The key is a real
    secret (unlike the pin), hence ``hmac.compare_digest``.
    """
    if not presented:
        return False
    return hmac.compare_digest(_normalize(expected).encode(), _normalize(presented).encode())


def _normalize(value: str) -> str:
    """Uppercase and strip everything that isn't ``[0-9A-Z]``."""
    return _NON_ALNUM.sub("", value.upper())
