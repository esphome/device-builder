"""HTTP bundle-upload endpoint + its capability tokens."""

from __future__ import annotations

import logging
import secrets
import time

from aiohttp import web

from ...helpers.api import CommandError
from ...helpers.bundle_limits import BUNDLE_MAX_TOTAL_BYTES

_LOGGER = logging.getLogger(__name__)

_UPLOAD_CHUNK_BYTES = 64 * 1024


class UploadTokens:
    """Single-use, short-TTL capability tokens authorizing an HTTP bundle upload.

    Minted over the authenticated WebSocket (``devices/import_bundle_token``)
    and consumed by ``POST /api/devices/import_bundle``, which is the route's
    sole authorization (it's in ``auth_middleware``'s public allowlist). A
    ``fetch`` POST can't easily carry the session bearer, so the token stands
    in — unguessable (:mod:`secrets`), fast-expiring, and single-use.
    """

    def __init__(self, ttl_seconds: float = 60.0) -> None:
        self._ttl = ttl_seconds
        self._tokens: dict[str, float] = {}

    def create(self) -> str:
        self._purge()
        token = secrets.token_urlsafe(32)
        self._tokens[token] = time.monotonic() + self._ttl
        return token

    def consume(self, token: str) -> bool:
        """Pop a token (single-use); False if unknown, already-used, or expired."""
        expiry = self._tokens.pop(token, None)
        return expiry is not None and time.monotonic() <= expiry

    def _purge(self) -> None:
        now = time.monotonic()
        for token in [t for t, exp in self._tokens.items() if now > exp]:
            del self._tokens[token]


async def http_import_bundle(request: web.Request) -> web.StreamResponse:
    """``POST /api/devices/import_bundle?token=&mode=`` — import a bundle upload.

    HTTP (not WebSocket) so a large bundle isn't capped by a proxy's WebSocket
    ``max_msg_size``. The body streams straight in; ``mode=resolve`` with
    repeated ``overwrite`` params resolves conflicts, anything else detects
    them. Returns an ``ImportBundleResponse`` as JSON.
    """
    db = request.app["device_builder"]
    if not db.devices.import_tokens.consume(request.query.get("token", "")):
        raise web.HTTPNotFound
    # Stream the raw body rather than request.read(), which would enforce the
    # app-wide 1 MiB client_max_size; cap it ourselves against the shared limit.
    buf = bytearray()
    async for chunk in request.content.iter_chunked(_UPLOAD_CHUNK_BYTES):
        buf += chunk
        if len(buf) > BUNDLE_MAX_TOTAL_BYTES:
            limit_mb = BUNDLE_MAX_TOTAL_BYTES // (1024 * 1024)
            raise web.HTTPRequestEntityTooLarge(
                max_size=BUNDLE_MAX_TOTAL_BYTES,
                actual_size=len(buf),
                text=f"Bundle exceeds the {limit_mb} MB upload limit.",
            )
    overwrite = (
        request.query.getall("overwrite", []) if request.query.get("mode") == "resolve" else None
    )
    try:
        result = await db.devices.import_bundle(bundle_bytes=bytes(buf), overwrite=overwrite)
    except CommandError as err:
        return web.json_response({"error_code": str(err.code), "details": err.message}, status=400)
    return web.json_response(result.to_dict())
