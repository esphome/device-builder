"""
Project-local custom :mod:`voluptuous` validators.

Voluptuous lives in our dependency closure (transitive via
ESPHome's ``config_validation``) and is the natural choice for
declarative field validation on dataclass schemas in this
project. Anything we need that the upstream library doesn't
already ship lands here so each consumer pulls from one place
rather than reinventing the wrapper.

Each validator is a callable suitable for use inside
``vol.Schema`` / ``vol.All`` chains: it accepts a value and
either returns the (possibly normalised) value or raises
``vol.Invalid``.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol


def not_bool(value: Any) -> Any:
    """
    Reject :class:`bool` values, return everything else unchanged.

    Voluptuous's ``int`` check accepts ``bool`` — Python's
    ``isinstance(True, int)`` is true — so a schema like
    ``vol.All(int, vol.Range(min=1, max=65535))`` would silently
    coerce ``True`` to ``1`` and ``False`` to ``0``. Chain
    :func:`not_bool` *before* the ``int`` check (e.g. for ports,
    refcounts, anything where a stray boolean would be a
    user-error rather than the intended type) so the rejection
    happens at validation time with a legible message rather
    than landing as a wrong-but-valid integer downstream.
    """
    if isinstance(value, bool):
        raise vol.Invalid("must not be a bool")
    return value


def lowercase_hex(length: int) -> vol.All:
    """
    Build a validator for a ``length``-char lowercase-hex string.

    Returns a :class:`vol.All` chain that asserts the value is
    a string of exactly ``length`` characters drawn from
    ``[0-9a-f]``. Used for SHA-256 hashes (``length=64``,
    ``pin_sha256`` on every peer-link row, the deleted
    bearer-secret hash, etc.) + any other lowercase-hex digest
    surface.

    The :class:`vol.Length` chain element is redundant with
    the regex's ``{length}`` quantifier but kept as a defensive
    belt — a future regex tweak that accidentally widened the
    alphabet (or dropped the anchors) would still get caught
    by the explicit length check. The caller pays one extra
    tiny check at validation time; the readability of "length
    + alphabet, both pinned" wins.

    Returns ``vol.All`` rather than the bare regex so the
    chain is callable directly inside a ``vol.Schema``
    field-position without an extra ``vol.All(...)`` wrap at
    each call site.
    """
    pattern = rf"^[0-9a-f]{{{length}}}$"
    return vol.All(str, vol.Length(min=length, max=length), vol.Match(pattern))
