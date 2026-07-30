"""Migration-rule kinds and the extra fields each requires beyond old/new.

Stdlib-only — ``script/validate_definitions.py`` imports this from the
pre-commit env.
"""

from __future__ import annotations

#: Shapes the generic rules deliberately don't reach — check when the
#: first real pair lands: flow-style list items, and the legacy
#: bare-mapping ``ota:`` / ``time:`` form (implicit platform).
MIGRATION_RULE_EXTRA_FIELDS: dict[str, tuple[str, ...]] = {
    "component_block_field": ("component",),
    "platform_item_field": ("domain", "platform"),
    "component_key": (),
}
