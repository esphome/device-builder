"""Receiver-side ``get_settings`` / ``set_settings`` WS commands."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING

from ...helpers.api import CommandError
from ...models import (
    MAX_CLEANUP_TTL_SECONDS,
    MIN_CLEANUP_TTL_SECONDS,
    ErrorCode,
    RemoteBuildSettings,
    RemoteBuildSettingsView,
)
from ..config import remote_build_settings_transaction

if TYPE_CHECKING:
    from .receiver import ReceiverController


async def get_settings(controller: ReceiverController) -> RemoteBuildSettingsView:
    """Return the receiver-side remote-build settings (wire view)."""
    return to_view(controller, await controller._load_settings_async())


def to_view(
    controller: ReceiverController, settings: RemoteBuildSettings
) -> RemoteBuildSettingsView:
    """Project receiver settings to wire view, merging in-memory peers.

    The peer list is RAM-canonical: PENDING entries live in
    ``state.pending_peers`` for the active pairing window's
    lifetime (never hit disk) and APPROVED entries live in
    ``state.approved_peers`` / its per-file ``Store``.
    ``settings`` is consulted for the master ``enabled``
    toggle.
    """
    return RemoteBuildSettingsView(
        enabled=settings.enabled,
        cleanup_ttl_seconds=settings.cleanup_ttl_seconds,
        peers=controller._peer_summaries(),
    )


async def modify_settings(
    controller: ReceiverController,
    mutator: Callable[[RemoteBuildSettings], None],
) -> RemoteBuildSettingsView:
    """
    Run ``mutator`` against the current settings and persist the result.

    Wraps :func:`remote_build_settings_transaction` so the
    whole read-modify-write happens under the metadata lock,
    so two concurrent callers can't both read the same starting
    value and have the second save wipe the first's change.
    Runs in the default executor since the transaction does
    blocking JSON I/O. Returns the wire view so the response
    leaving this method can never carry ``secret_sha256``.

    ``mutator`` is invoked with the freshly-loaded settings
    and is expected to mutate it in place. A
    :class:`CommandError` raised inside the mutator (e.g.
    duplicate-detection on add) propagates out and discards
    the pending write; same exception-on-discard contract as
    :func:`metadata_transaction`.
    """

    def _txn() -> RemoteBuildSettings:
        with remote_build_settings_transaction(controller._db.settings.config_dir) as settings:
            mutator(settings)
            return settings

    loop = asyncio.get_running_loop()
    settings = await loop.run_in_executor(None, _txn)
    return to_view(controller, settings)


async def set_settings(
    controller: ReceiverController,
    *,
    enabled: bool,
    cleanup_ttl_seconds: int | None = None,
) -> RemoteBuildSettingsView:
    """
    Persist the receiver-side ``enabled`` master switch.

    Read-modify-write so peers / other fields stay intact.
    Strict-bool validation defeats truthiness coercion on
    this security-sensitive toggle.

    Optional ``cleanup_ttl_seconds`` updates the cleanup
    sweep threshold, range-checked against
    :data:`MIN_CLEANUP_TTL_SECONDS` /
    :data:`MAX_CLEANUP_TTL_SECONDS`. Omit to keep current.

    Live-rebinds the peer-link listener: True runs the
    startup bind path, False tears down + clears the mDNS
    pin/port advertise. Fail-soft on bind error.
    """
    if not isinstance(enabled, bool):
        msg = "remote_build/set_settings: 'enabled' must be a boolean"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    if cleanup_ttl_seconds is not None:
        # bool subclasses int, so reject ``True`` first to
        # avoid a misleading OUT_OF_RANGE on a type error.
        if isinstance(cleanup_ttl_seconds, bool) or not isinstance(cleanup_ttl_seconds, int):
            msg = "remote_build/set_settings: 'cleanup_ttl_seconds' must be an integer"
            raise CommandError(ErrorCode.INVALID_ARGS, msg)
        if not MIN_CLEANUP_TTL_SECONDS <= cleanup_ttl_seconds <= MAX_CLEANUP_TTL_SECONDS:
            msg = (
                f"remote_build/set_settings: 'cleanup_ttl_seconds' must be between "
                f"{MIN_CLEANUP_TTL_SECONDS} and {MAX_CLEANUP_TTL_SECONDS}"
            )
            raise CommandError(ErrorCode.INVALID_ARGS, msg)

    def _set(settings: RemoteBuildSettings) -> None:
        settings.enabled = enabled
        if cleanup_ttl_seconds is not None:
            settings.cleanup_ttl_seconds = cleanup_ttl_seconds

    view = await modify_settings(controller, _set)
    await controller._db.apply_remote_build_enabled()
    return view


async def current_settings_view(
    controller: ReceiverController,
) -> RemoteBuildSettingsView:
    """Load settings from disk and project to the wire view (post-mutation response)."""
    return to_view(controller, await controller._load_settings_async())
