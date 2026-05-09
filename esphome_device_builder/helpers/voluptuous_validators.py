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
