"""Decode the legacy sidecar ``_preferences`` blob (read once at migration)."""

from __future__ import annotations

from typing import Any

from ...models import UserPreferences

_PREFS_KEY = "_preferences"


def _prefs_from_data(data: dict[str, Any]) -> UserPreferences:
    """Decode the ``_preferences`` blob, returning defaults on a corrupt shape."""
    try:
        return UserPreferences.from_dict(data.get(_PREFS_KEY, {}))
    except (ValueError, TypeError, LookupError):
        return UserPreferences()
