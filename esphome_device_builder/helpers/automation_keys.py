"""Shared classifier for inline automation trigger keys."""

from __future__ import annotations

# Key-name prefixes marking an inline automation *trigger* (``on_press``,
# ``on_value``, ``on_state_change``, ...). A ``type: trigger`` config-var
# whose key lacks this prefix is a component action-field (``set_action``,
# ``open_action``, ``*_mode``) the component performs on command — edited
# through the component form's action-list surface, not the trigger picker.
TRIGGER_KEY_PREFIXES: tuple[str, ...] = ("on_",)


def is_trigger_key(key: str) -> bool:
    """Return True when *key* names an inline automation trigger (``on_*``)."""
    return key.startswith(TRIGGER_KEY_PREFIXES)
