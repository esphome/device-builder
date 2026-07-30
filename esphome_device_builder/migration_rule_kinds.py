"""Migration-rule kinds and the extra fields each requires beyond old/new.

Stdlib-only: the single source for the runtime loader, the sync
emitter, and ``script/validate_definitions.py`` (whose pre-commit env
has no ``orjson``, so it can't import ``definitions``).
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
