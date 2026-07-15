"""Read this dashboard's advertised display identity for the peer link."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...helpers.dashboard_advertise import default_friendly_name

if TYPE_CHECKING:
    from ...device_builder import DeviceBuilder


def dashboard_display_identity(db: DeviceBuilder) -> tuple[str, bool]:
    """
    Return ``(friendly_name, ha_addon)`` for this dashboard.

    Reads the running :class:`DashboardAdvertiser` (the same values
    published in mDNS TXT); falls back to the hostname-derived
    default when zeroconf never came up.
    """
    advertiser = db.dashboard_advertiser
    friendly = advertiser.friendly_name if advertiser is not None else default_friendly_name()
    return friendly, db.settings.on_ha_addon
