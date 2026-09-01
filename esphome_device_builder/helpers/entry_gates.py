"""Config-entry gate predicate shared by the runtime validator and the sync scripts."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any


def entry_gate_active(
    fields: Mapping[str, Any],
    *,
    depends_on: str | None,
    depends_on_value: Any = None,
    depends_on_value_not: Any = None,
    depends_on_value_any: Collection[Any] | None = None,
) -> bool:
    """Whether a config entry's ``depends_on`` value gate is satisfied by *fields*."""
    if depends_on is None:
        return True
    dep = fields.get(depends_on)
    if depends_on_value is not None:
        return bool(dep == depends_on_value)
    if depends_on_value_not is not None:
        return bool(dep != depends_on_value_not)
    if depends_on_value_any is not None:
        return dep in depends_on_value_any
    return True
