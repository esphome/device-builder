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

from ...helpers.api import CommandError
from ...models import (
    ErrorCode,
    EventType,
    OffloaderAllowMajorVersionMismatchChangedData,
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
        allow_major_version_mismatch=controller.state.allow_major_version_mismatch,
    )


async def get_offloader_settings(
    controller: OffloaderController,
) -> OffloaderRemoteBuildSettingsView:
    """Return the offloader-side settings view (master toggles + pairings list)."""
    return offloader_settings_view(controller)


async def set_offloader_settings(
    controller: OffloaderController,
    *,
    remote_builds_enabled: bool | None = None,
    allow_major_version_mismatch: bool | None = None,
) -> OffloaderRemoteBuildSettingsView:
    """
    Flip one or both offloader-side master toggles.

    Passing ``None`` (or omitting) leaves that flag untouched;
    each flipped flag fires its own event. Refusing the
    all-``None`` call keeps a frontend bug from silently
    no-op'ing.
    """
    if remote_builds_enabled is None and allow_major_version_mismatch is None:
        msg = (
            "remote_build/set_offloader_settings: at least one of "
            "remote_builds_enabled or allow_major_version_mismatch must be supplied"
        )
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    save_needed = False
    if remote_builds_enabled is not None:
        clean_remote_builds_enabled = validate_bool(
            remote_builds_enabled,
            command="remote_build/set_offloader_settings",
            field="remote_builds_enabled",
        )
        controller.state.remote_builds_enabled = clean_remote_builds_enabled
        toggled: OffloaderRemoteBuildsToggledData = {
            "remote_builds_enabled": clean_remote_builds_enabled,
        }
        controller._db.bus.fire(EventType.OFFLOADER_REMOTE_BUILDS_TOGGLED, toggled)
        save_needed = True
    if allow_major_version_mismatch is not None:
        clean_allow_mismatch = validate_bool(
            allow_major_version_mismatch,
            command="remote_build/set_offloader_settings",
            field="allow_major_version_mismatch",
        )
        controller.state.allow_major_version_mismatch = clean_allow_mismatch
        gate: OffloaderAllowMajorVersionMismatchChangedData = {
            "allow_major_version_mismatch": clean_allow_mismatch,
        }
        controller._db.bus.fire(EventType.OFFLOADER_ALLOW_MAJOR_VERSION_MISMATCH_CHANGED, gate)
        save_needed = True
    if save_needed:
        controller._schedule_pairings_save()
    return offloader_settings_view(controller)
