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
from ...helpers.version_compat import VersionMatchPolicy
from ...models import (
    ErrorCode,
    EventType,
    OffloaderIncludeLocalChangedData,
    OffloaderRemoteBuildSettingsView,
    OffloaderRemoteBuildsToggledData,
    OffloaderVersionMatchPolicyChangedData,
)
from ._validators import validate_bool

if TYPE_CHECKING:
    from .offloader import OffloaderController


def offloader_settings_view(
    controller: OffloaderController,
) -> OffloaderRemoteBuildSettingsView:
    """Project the in-RAM offloader-side state to its wire view."""
    return OffloaderRemoteBuildSettingsView(
        pairings=controller.pairings_snapshot(),
        remote_builds_enabled=controller.state.remote_builds_enabled,
        version_match_policy=controller.state.version_match_policy,
        include_local_in_pool=controller.state.include_local_in_pool,
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
    version_match_policy: str | None = None,
    include_local_in_pool: bool | None = None,
) -> OffloaderRemoteBuildSettingsView:
    """
    Flip one or more offloader-side master settings.

    Passing ``None`` (or omitting) leaves that field untouched;
    each changed field fires its own event. Refusing the
    all-``None`` call keeps a frontend bug from silently
    no-op'ing.
    """
    if (
        remote_builds_enabled is None
        and version_match_policy is None
        and include_local_in_pool is None
    ):
        msg = (
            "remote_build/set_offloader_settings: at least one of "
            "remote_builds_enabled, version_match_policy or include_local_in_pool "
            "must be supplied"
        )
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    # Validate both args before mutating anything so a bad
    # version_match_policy can't half-apply: an earlier shape
    # mutated remote_builds_enabled then raised on the policy
    # validator, leaving RAM / disk / cross-tab subscribers out
    # of sync with each other.
    clean_remote_builds_enabled = (
        validate_bool(
            remote_builds_enabled,
            command="remote_build/set_offloader_settings",
            field="remote_builds_enabled",
        )
        if remote_builds_enabled is not None
        else None
    )
    clean_policy = (
        _validate_version_match_policy(version_match_policy)
        if version_match_policy is not None
        else None
    )
    clean_include_local = (
        validate_bool(
            include_local_in_pool,
            command="remote_build/set_offloader_settings",
            field="include_local_in_pool",
        )
        if include_local_in_pool is not None
        else None
    )
    # Per-field equality guards so the event + save only fire on
    # actual state changes — keeps the "event fired ⇒ value
    # changed" invariant other controllers in this repo uphold
    # and avoids debouncer churn on idempotent writes.
    save_needed = False
    if (
        clean_remote_builds_enabled is not None
        and clean_remote_builds_enabled != controller.state.remote_builds_enabled
    ):
        controller.state.remote_builds_enabled = clean_remote_builds_enabled
        toggled: OffloaderRemoteBuildsToggledData = {
            "remote_builds_enabled": clean_remote_builds_enabled,
        }
        controller._db.bus.fire(EventType.OFFLOADER_REMOTE_BUILDS_TOGGLED, toggled)
        save_needed = True
    if clean_policy is not None and clean_policy is not controller.state.version_match_policy:
        controller.state.version_match_policy = clean_policy
        changed: OffloaderVersionMatchPolicyChangedData = {
            "version_match_policy": clean_policy,
        }
        controller._db.bus.fire(EventType.OFFLOADER_VERSION_MATCH_POLICY_CHANGED, changed)
        save_needed = True
    if (
        clean_include_local is not None
        and clean_include_local != controller.state.include_local_in_pool
    ):
        controller.state.include_local_in_pool = clean_include_local
        include_local_changed: OffloaderIncludeLocalChangedData = {
            "include_local_in_pool": clean_include_local,
        }
        controller._db.bus.fire(EventType.OFFLOADER_INCLUDE_LOCAL_CHANGED, include_local_changed)
        save_needed = True
    if save_needed:
        controller._schedule_pairings_save()
    return offloader_settings_view(controller)


def _validate_version_match_policy(raw: object) -> VersionMatchPolicy:
    """Coerce a wire ``version_match_policy`` value to its enum member."""
    if not isinstance(raw, str):
        msg = "remote_build/set_offloader_settings: 'version_match_policy' must be a string"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    try:
        return VersionMatchPolicy(raw)
    except ValueError as exc:
        msg = (
            "remote_build/set_offloader_settings: 'version_match_policy' must be one of "
            f"{sorted(v.value for v in VersionMatchPolicy)}; got {raw!r}"
        )
        raise CommandError(ErrorCode.INVALID_ARGS, msg) from exc
