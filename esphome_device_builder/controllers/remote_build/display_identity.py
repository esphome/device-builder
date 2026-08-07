"""Read this dashboard's advertised display identity for the peer link."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...helpers.dashboard_advertise import advertised_friendly_name

if TYPE_CHECKING:
    from ...device_builder import DeviceBuilder


def dashboard_display_identity(db: DeviceBuilder) -> tuple[str, bool]:
    """Return ``(friendly_name, ha_addon)`` for this dashboard."""
    return advertised_friendly_name(db.dashboard_advertiser), db.settings.on_ha_addon
