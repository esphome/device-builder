"""Hub references of I/O-expander pins, shared by the board and device sync scripts."""

from __future__ import annotations

import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)


def expander_hub_ref(hub: Any) -> str | None:
    """Return *hub*'s ref: the hub id, ``@0x44`` for ``{address: 0x44}``, else ``None``."""
    if isinstance(hub, str):
        return hub or None
    if isinstance(hub, dict) and hub.keys() == {"address"}:
        return address_hub_ref(hub["address"])
    return None


def address_hub_ref(address: Any) -> str | None:
    """Return the ``@0x..`` ref of an I2C *address* as esphome validates it, else ``None``."""
    import esphome.config_validation as cv

    try:
        value = cv.i2c_address(address)
    except cv.Invalid:
        return None
    return f"@0x{value:02x}"


def is_address_ref(ref: str) -> bool:
    """Whether *ref* selects its hub by I2C address rather than by id."""
    return ref.startswith("@")


def ref_address(ref: str) -> int:
    """Return the I2C address an address ref (``@0x44``) selects."""
    return int(ref[1:], 16)


def match_address_block(
    blocks: list[dict[str, Any]], ref: str, default_address: Any = None
) -> dict[str, Any] | None:
    """Return the sole block at address *ref*; a block without ``address`` sits at *default_address*."""
    matches = [b for b in blocks if address_hub_ref(b.get("address", default_address)) == ref]
    if len(matches) > 1:
        _LOGGER.warning(
            "%d hub blocks share address %s; selector is ambiguous", len(matches), ref[1:]
        )
    return matches[0] if len(matches) == 1 else None


def lock_hub_identity(
    fields: dict[str, Any], instance_id: str | None, block: dict[str, Any]
) -> str | None:
    """Lock the hub ``id`` and/or ``address`` a pin ref resolves against; return the upstream id."""
    upstream_id: Any = instance_id
    if instance_id is not None and is_address_ref(instance_id):
        fields["address"] = {"value": ref_address(instance_id), "locked": True}
        upstream_id = block.get("id")
    if not isinstance(upstream_id, str) or not upstream_id:
        return None
    fields["id"] = {"value": upstream_id, "locked": True}
    return upstream_id


def catalog_address_default(component: dict[str, Any] | None) -> Any:
    """Return the catalog ``default_value`` of *component*'s ``address`` entry, else ``None``."""
    for ce in (component or {}).get("config_entries") or []:
        if ce.get("key") == "address":
            return ce.get("default_value")
    return None


def merge_alias_refs(
    aliases: dict[tuple[str, str | None], tuple[str, str | None]],
    hub_prereqs: dict[tuple[str, str | None], list[str]],
    lifted_hubs: dict[tuple[str, str | None], tuple[dict[str, Any], dict[str, Any]]],
) -> None:
    """Point each alias ref at its first lift's prerequisites and lock its identity there too."""
    for ref, first in aliases.items():
        if first in hub_prereqs:
            hub_prereqs[ref] = hub_prereqs[first]
            fields, block = lifted_hubs[first]
            lock_hub_identity(fields, ref[1], block)
