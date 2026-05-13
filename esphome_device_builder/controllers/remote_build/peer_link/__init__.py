"""
Peer-link Noise WS handler for the remote-build feature (issue #106).

Owns the wire shape of the
``/remote-build/peer-link`` WebSocket endpoint: drives the
``Noise_XX_25519_ChaChaPoly_SHA256`` handshake, parses the
offloader's ``intent`` discriminator out of the cleartext msg1
payload + the encrypted msg3 payload, dispatches to the
controller's helper methods (`record_pair_request` /
`lookup_peer_for_session` / `lookup_peer_for_status`), and wraps
the response in a ChaCha20-Poly1305 transport frame.

Handshake-payload confidentiality (per the Noise XX wire spec
that ``helpers.peer_link_noise`` documents):

* msg1 (offloader → receiver, plaintext): ``{"intent": "..."}``.
  Coarse discriminator only; sensitive fields wait until msg3.
* msg2 (receiver → offloader, encrypted with the freshly-mixed
  ``ee`` + ``es`` chain): empty payload. The encryption + the
  carried responder static key are what the offloader pins
  against in the ``preview`` flow.
* msg3 (offloader → receiver, encrypted with the now-finalized
  cipher): ``{"dashboard_id": "...", "label": "..."}`` for
  pair_request; ``{"dashboard_id": "..."}`` for peer_link /
  pair_status; empty for preview.

After the handshake completes, the receiver sends one
post-handshake transport frame carrying
``{"intent_response": "..."}``. For ``intent="preview"`` /
``"pair_request"`` / ``"pair_status"`` the receiver then closes
the WS — those intents are one-shot. For ``intent="peer_link"``
on a successful auth (``IntentResponse.OK``), the receiver
*keeps the WS open* and runs a long-lived application session
on top of the same Noise transport: every subsequent frame is
JSON-encoded then ChaCha20-Poly1305-encrypted via
:meth:`PeerLinkNoiseSession.encrypt` /
:meth:`PeerLinkNoiseSession.decrypt`. The session loop runs an
encrypted ``ping`` / ``pong`` heartbeat (30s tick + 90s miss
threshold) and a controller-side session registry dedupes by
``dashboard_id`` (a duplicate connect kicks the older session
via a ``terminate`` frame so a restarted offloader takes over
its previous slot rather than doubling). Application message
types (``submit_job``, ``job_state_changed``, ``queue_status``,
…) ride on top of the same Noise transport.

Timeouts: handshake reads have an explicit timeout so a peer that
opens a TCP connection and never sends the first frame can't pin
a coroutine forever. The timeout is generous (10s) because the
Noise XX handshake itself is local-DH cheap; only the network
round-trip costs anything, and that's bounded by LAN latency.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from ....api.ws import WEBSOCKETS_KEY
from ....helpers.peer_link_identity import get_or_create_peer_link_identity

# Redundant aliases mark these as intentional re-exports for both
# ruff (F401) and mypy (no-redef) — preserves external imports like
# ``from .peer_link import TerminateReason`` without an ``__all__``
# that would also accidentally narrow ``import *`` semantics. Tests
# import the underscore-prefixed handshake-driver + session symbols
# this way.
from .channel import PeerLinkChannel as PeerLinkChannel
from .handshake import _dispatch_intent as _dispatch_intent
from .handshake import _DispatchInput as _DispatchInput
from .handshake import _drive_peer_link_session as _drive_peer_link_session
from .handshake import _HandshakeStep as _HandshakeStep
from .session import APP_FRAME_MAX_BYTES as APP_FRAME_MAX_BYTES
from .session import HEARTBEAT_DEAD_AFTER_SECONDS as HEARTBEAT_DEAD_AFTER_SECONDS
from .session import HEARTBEAT_INTERVAL_SECONDS as HEARTBEAT_INTERVAL_SECONDS
from .session import HEARTBEAT_MISS_THRESHOLD as HEARTBEAT_MISS_THRESHOLD
from .session import PeerLinkSession as PeerLinkSession
from .session import _run_peer_link_session as _run_peer_link_session
from .session import parse_app_frame as parse_app_frame
from .session import run_peer_link_heartbeat as run_peer_link_heartbeat
from .wire import AppMessageType as AppMessageType
from .wire import TerminateReason as TerminateReason

if TYPE_CHECKING:
    from ..receiver import ReceiverController

_LOGGER = logging.getLogger(__name__)

PEER_LINK_PATH = "/remote-build/peer-link"


async def make_peer_link_handler(
    controller: ReceiverController,
    config_dir: Path,
) -> Callable[[web.Request], Awaitable[web.WebSocketResponse]]:
    """
    Build the aiohttp handler for ``/remote-build/peer-link``.

    Loads the X25519 peer-link identity once at handler-factory
    time and captures it in the closure so each incoming WS
    connection constructs its ``PeerLinkNoiseSession`` from
    already-loaded bytes instead of hitting disk + an executor
    hop on every handshake. Identity is stable for the process
    lifetime; rotation tears down + rebuilds the runner, which
    re-enters this factory.

    ``config_dir`` is passed in explicitly rather than read off
    the controller's private ``_db`` chain — the caller
    (``DeviceBuilder._build_and_start_remote_build_runner``)
    already has it in hand, and a sibling module reaching
    through ``controller._db.settings.config_dir`` would be
    a single-leading-underscore boundary violation.
    """
    loop = asyncio.get_running_loop()
    identity = await loop.run_in_executor(None, get_or_create_peer_link_identity, config_dir)
    identity_priv = identity.private_bytes

    async def handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        # Register on the peer-link app's WS set so the shared
        # ``close_active_websockets`` shutdown hook can unblock
        # this handler instead of pinning ``runner.cleanup()`` to
        # aiohttp's 60s ``shutdown_timeout`` while an idle paired
        # offloader sits in ``async for msg in ws``. The set is
        # seeded in :meth:`DeviceBuilder._build_and_start_remote_build_runner`
        # at construction time; tests that build a hand-rolled
        # peer-link app are expected to seed it themselves.
        request.app[WEBSOCKETS_KEY].add(ws)
        peer_ip = request.remote or ""
        try:
            await _drive_peer_link_session(controller, ws, peer_ip, identity_priv)
        except Exception:
            _LOGGER.exception("peer-link session error from %s", peer_ip)
        finally:
            if not ws.closed:
                await ws.close()
        return ws

    return handler
