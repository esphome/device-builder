"""User-preferences persistence backed by the metadata sidecar."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ...models import UserPreferences
from .metadata import _load_metadata, metadata_transaction

_PREFS_KEY = "_preferences"


def _prefs_from_data(data: dict[str, Any]) -> UserPreferences:
    """Decode the ``_preferences`` blob, returning defaults on a corrupt shape."""
    try:
        return UserPreferences.from_dict(data.get(_PREFS_KEY, {}))
    except (ValueError, TypeError, LookupError):
        return UserPreferences()


def load_preferences(config_dir: Path) -> UserPreferences:
    """Load user preferences, returning defaults for missing or corrupt fields."""
    return _prefs_from_data(_load_metadata(config_dir))


def save_preferences(config_dir: Path, prefs: UserPreferences) -> None:
    """Save user preferences to disk."""
    with metadata_transaction(config_dir) as data:
        data[_PREFS_KEY] = prefs.to_dict()


def mutate_preferences(
    config_dir: Path, mutate: Callable[[UserPreferences], UserPreferences | None]
) -> UserPreferences:
    """
    Atomic read-modify-write for user preferences.

    *mutate* receives the current prefs (defaults on a corrupt
    blob) and returns the next state, or mutates it in place and
    returns ``None``. Load and save share one lock.
    """
    with metadata_transaction(config_dir) as data:
        prefs = _prefs_from_data(data)
        result = mutate(prefs)
        if result is None:
            result = prefs
        data[_PREFS_KEY] = result.to_dict()
        return result


def update_preferences(config_dir: Path, update_fields: dict[str, Any]) -> UserPreferences:
    """Merge a validated partial dict into stored preferences atomically."""
    return mutate_preferences(
        config_dir,
        lambda prefs: UserPreferences.from_dict({**prefs.to_dict(), **update_fields}),
    )
