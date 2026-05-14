"""
Offloader-side ``get_offloader_settings`` / ``set_offloader_settings`` WS commands.

The two settings WS commands plus their shared
:func:`offloader_settings_view` projection. Bodies take
:class:`OffloaderController` as the first arg; the controller
keeps the two ``@api_command``-decorated methods as thin
bound-method delegates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...models import (
    EventType,
    OffloaderRemoteBuildSettingsView,
    OffloaderRemoteBuildsToggledData,
)
from ._validators import validate_bool

if TYPE_CHECKING:
    from .offloader import OffloaderController


def offloader_settings_view(
    controller: OffloaderController,
) -> OffloaderRemoteBuildSettingsView:
    """Project the in-RAM offloader-side state to its wire view.

    Pure sync RAM read off :attr:`_pairings` +
    :attr:`_remote_builds_enabled`, which are canonical
    after :meth:`OffloaderController.start` seeds them from
    disk.
    """
    return OffloaderRemoteBuildSettingsView(
        pairings=controller.pairings_snapshot(),
        remote_builds_enabled=controller.state.remote_builds_enabled,
    )


async def get_offloader_settings(
    controller: OffloaderController,
) -> OffloaderRemoteBuildSettingsView:
    """Return the offloader-side settings view (master toggle + pairings list)."""
    return offloader_settings_view(controller)


async def set_offloader_settings(
    controller: OffloaderController, *, remote_builds_enabled: bool
) -> OffloaderRemoteBuildSettingsView:
    """
    Flip the offloader-side master toggle for transparent install.

    ``False`` short-circuits :func:`pick_build_path` to
    LOCAL; peer-link sessions stay open and the manual
    Send-builds dialog still works. The intent is "keep the
    pairings but stop auto-routing for now."

    Fires ``OFFLOADER_REMOTE_BUILDS_TOGGLED`` for cross-tab
    sync; debounce-saves through ``_pairings_store`` (same
    on-disk shape).
    """
    controller.state.remote_builds_enabled = validate_bool(
        remote_builds_enabled,
        command="remote_build/set_offloader_settings",
        field="remote_builds_enabled",
    )
    toggled: OffloaderRemoteBuildsToggledData = {
        "remote_builds_enabled": remote_builds_enabled,
    }
    controller._db.bus.fire(EventType.OFFLOADER_REMOTE_BUILDS_TOGGLED, toggled)
    controller._schedule_pairings_save()
    return offloader_settings_view(controller)
