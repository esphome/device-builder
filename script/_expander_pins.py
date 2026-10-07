"""Hub references of I/O-expander pins, shared by the board and device sync scripts."""

from __future__ import annotations

from typing import Any


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
