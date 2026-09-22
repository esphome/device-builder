"""Shared classifiers for automation body keys."""

from __future__ import annotations

from ..models.automations import AutomationAction, AutomationCondition

# Key-name prefixes marking an inline automation *trigger* (``on_press``,
# ``on_value``, ``on_state_change``, ...). A ``type: trigger`` config-var
# whose key lacks this prefix is a component action-field (``set_action``,
# ``open_action``, ``*_mode``) the component performs on command — edited
# through the component form's action-list surface, not the trigger picker.
TRIGGER_KEY_PREFIXES: tuple[str, ...] = ("on_",)

# Action-body keys that introduce a condition gate rather than plain params.
CONDITION_GATE_KEYS: frozenset[str] = frozenset({"condition", "all", "any"})

# Fallback collapse key for an entry with no usable scalar shorthand.
DEFAULT_SHORTHAND_KEY = "id"


def is_trigger_key(key: str) -> bool:
    """Return True when *key* names an inline automation trigger (``on_*``)."""
    return key.startswith(TRIGGER_KEY_PREFIXES)


def bare_trigger_key(trigger_id: str) -> str:
    """Return the ``on_*`` YAML key a catalog trigger id ends in."""
    return trigger_id.rsplit(".", 1)[-1]


def shorthand_key(entry: AutomationAction | AutomationCondition | None) -> str | None:
    """Return the key a bare scalar collapses to, ``None`` when the entry has no scalar form."""
    if entry is None:
        return None
    action_lists = entry.accepts_action_list if isinstance(entry, AutomationAction) else ()
    key = entry.scalar_shorthand_key
    if key and key not in CONDITION_GATE_KEYS and key not in action_lists:
        return key
    if any(e.key == DEFAULT_SHORTHAND_KEY for e in entry.config_entries):
        return None
    return DEFAULT_SHORTHAND_KEY


def scalar_param_key(entry: AutomationAction | AutomationCondition) -> str:
    """Return the param key a parsed bare scalar is stored under."""
    return shorthand_key(entry) or DEFAULT_SHORTHAND_KEY
