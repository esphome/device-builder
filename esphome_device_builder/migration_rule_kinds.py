"""Migration-rule kinds and the extra fields each requires beyond old/new.

Stdlib-only — ``script/validate_definitions.py`` imports this from the
pre-commit env.
"""

from __future__ import annotations

#: Extra non-empty string fields each rule kind requires beyond old/new.
MIGRATION_RULE_EXTRA_FIELDS: dict[str, tuple[str, ...]] = {
    "component_block_field": ("component",),
    "platform_item_field": ("domain", "platform"),
    "platform_channel_colors": ("domain", "platform"),
    "component_key": (),
}
