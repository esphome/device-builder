"""Receiver-side ``get_identity`` / ``rotate_identity`` WS commands."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ...helpers import dashboard_identity as _dashboard_identity_helper
from ...helpers.api import CommandError
from ...helpers.dashboard_identity import get_or_create_identity
from ...models import (
    ErrorCode,
    EventType,
    IdentityView,
    RemoteBuildIdentityRotatedData,
)
from ._summaries import identity_view

if TYPE_CHECKING:
    from .receiver import ReceiverController


async def get_identity(controller: ReceiverController) -> IdentityView:
    """Return this dashboard's stable identity (id + pin + versions + bind state).

    The X25519 private key is never returned; only
    ``pin_sha256`` (the fingerprint mDNS broadcasts and
    offloaders pin against).
    """
    identity = await get_or_create_identity(
        controller._db.settings.config_dir,
        controller._db.peer_link_identity_store,
    )
    return identity_view(identity, listener_bound=controller._db.is_remote_build_listener_bound)


async def rotate_identity(controller: ReceiverController) -> IdentityView:
    """
    Mint a fresh X25519 peer-link keypair, replacing whatever's on disk.

    Forces every paired offloader to re-pair — peers pinned
    on the old ``pin_sha256`` see a fingerprint mismatch on
    the next handshake. ``dashboard_id`` is preserved.

    Side effects when remote-build is currently bound:
    listener torn down + rebuilt with the fresh key,
    ``pin_sha256`` re-advertised in mDNS, rebuild fail-softs
    (``listener_bound=False`` in the response).
    :attr:`EventType.REMOTE_BUILD_IDENTITY_ROTATED` fires
    regardless of bind state so subscribers can refresh
    cached pins without polling.

    Concurrent calls return ``ALREADY_EXISTS`` — two
    rotations racing would each tear down + rebuild the
    listener; back-to-back is almost always an accidental
    double-click.
    """
    # Check+set is atomic on the single asyncio loop.
    if controller.state.rotation_in_flight:
        msg = "remote_build: an identity rotation is already in progress"
        raise CommandError(ErrorCode.ALREADY_EXISTS, msg)
    controller.state.rotation_in_flight = True
    return await asyncio.shield(_rotate_and_reload(controller))


async def _rotate_and_reload(controller: ReceiverController) -> IdentityView:
    """
    Rotate the keypair, rebind the listener, fire the bus event.

    The flag clear lives in this function's ``finally`` (not
    the caller's) so it tracks the shielded work; otherwise a
    cancelled awaiter would let a second request slip in
    mid-rebuild and double-fire the teardown.
    """
    try:
        identity = await _dashboard_identity_helper.rotate_identity(
            controller._db.settings.config_dir,
            controller._db.peer_link_identity_store,
        )
        listener_bound = await controller._db.reload_remote_build_identity(
            pin_sha256=identity.pin_sha256,
        )
        controller._db.bus.fire(
            EventType.REMOTE_BUILD_IDENTITY_ROTATED,
            RemoteBuildIdentityRotatedData(
                dashboard_id=identity.dashboard_id,
                pin_sha256=identity.pin_sha256,
            ),
        )
        return identity_view(identity, listener_bound=listener_bound)
    finally:
        controller.state.rotation_in_flight = False
