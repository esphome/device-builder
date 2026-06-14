"""Decode the legacy ``_preferences`` sidecar blob (the migration source).

Preferences are RAM-canonical behind ``PreferencesStore`` now; these are the
only bits that survive: the sidecar key + decoder the store reads once at
migration time. The sidecar *writers* were removed so nothing can reintroduce a
``_preferences`` blob that would diverge from the store.
"""

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
