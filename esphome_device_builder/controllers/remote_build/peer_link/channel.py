"""Shared peer-link application channel for both wire ends.

Receiver-side ``PeerLinkSession`` and offloader-side
``PeerLinkClient`` both compose around this so the
encrypt-and-send / parse-inbound / structured-terminate logic
lives in one place. ``ws`` is duck-typed (``send_bytes`` /
``close`` / async-iter); the same channel works against
aiohttp's server-side ``web.WebSocketResponse`` and client-side
``ClientWebSocketResponse``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from ....helpers import json as _json
from ....helpers.peer_link_noise import NOISE_ERRORS, PeerLinkNoiseSession
from .wire import AppMessageType
from .wire_io import _send_bytes_safely

_LOGGER = logging.getLogger(__name__)


@dataclass
class PeerLinkChannel:
    """
    Wire-level send / parse / terminate seam shared by both ends.

    Wraps the post-handshake :class:`PeerLinkNoiseSession` plus
    its WS endpoint and a send lock. Each side's session class
    composes one of these so the encrypt-then-send pattern (and
    the validate-decrypt-parse-dict-check parse pattern, and the
    structured terminate-frame-then-close pattern) only lives in
    one module. ``log_label`` is what callers want in their log
    lines: receiver passes its ``dashboard_id``, offloader
    passes ``"<hostname>:<port>"``.
    """

    noise: PeerLinkNoiseSession
    ws: Any  # WebSocketResponse | ClientWebSocketResponse — duck-typed (see class docstring)
    log_label: str
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send_frame(self, payload: dict[str, Any]) -> bool:
        """
        Encrypt *payload* under the send lock and send as a binary WS frame.

        Returns ``True`` on success, ``False`` on JSON-encode /
        Noise-encrypt / WS-side failure. The lock serialises
        concurrent callers (heartbeat + future application-message
        senders) so the Noise nonce advances in one direction only
        — the Noise cipher state is not safe to share across
        concurrent encrypts.
        """
        try:
            plaintext = _json.dumps(payload)
        except (TypeError, ValueError):
            _LOGGER.warning(
                "peer-link app frame for %s failed JSON encode", self.log_label, exc_info=True
            )
            return False
        async with self._send_lock:
            try:
                ciphertext = self.noise.encrypt(plaintext)
            except NOISE_ERRORS:
                _LOGGER.warning(
                    "peer-link app frame for %s failed Noise encrypt",
                    self.log_label,
                    exc_info=True,
                )
                return False
            return await _send_bytes_safely(self.ws, ciphertext, log_label="app frame")

    def parse_frame(self, msg: Any) -> dict[str, Any] | None:
        """
        Validate, decrypt, and JSON-parse one inbound frame.

        Thin wrapper around :func:`parse_app_frame` so callers
        don't have to thread :attr:`noise` and :attr:`log_label`
        through. See :func:`parse_app_frame` for the per-branch
        log + ``None``-on-malformed contract.
        """
        from . import parse_app_frame  # noqa: PLC0415

        return parse_app_frame(self.noise, msg, log_label=self.log_label)

    async def send_terminate(self, reason: str) -> None:
        """
        Send a structured ``terminate`` frame and close the WS, best-effort.

        The terminate frame routes through :meth:`send_frame` so
        the encrypt + lock invariants hold; the close that
        follows is best-effort because a peer that has already
        gone away won't accept either, and we want the call site
        idempotent across "WS still up" and "WS dead" states.
        Narrow suppress to transport-level errors only — including
        :class:`aiohttp.ClientError` because this channel runs on
        both sides of the wire (offloader side's ``self.ws`` is a
        :class:`aiohttp.ClientWebSocketResponse` whose ``.close()``
        can raise ``ClientConnectionError`` / ``ClientError``
        when the peer has already gone away). A ``ClientError``
        escaping here would block the caller's
        :class:`CancelledError` propagation when used inside a
        :meth:`PeerLinkClient._run_one_session` cancellation
        handler. Python 3.8+ already excludes ``CancelledError``
        from ``Exception``, so the wider suppression below stays
        compatible with the no-swallow contract.
        """
        await self.send_frame({"type": AppMessageType.TERMINATE.value, "reason": reason})
        with contextlib.suppress(OSError, RuntimeError, aiohttp.ClientError):
            await self.ws.close()
