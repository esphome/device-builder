"""HTTP bundle-upload endpoint + its capability tokens."""

from __future__ import annotations

import logging

from aiohttp import web

from ...helpers.api import CommandError
from ...helpers.bundle_limits import BUNDLE_MAX_TOTAL_BYTES
from ...helpers.capability_tokens import CapabilityTokens
from ...helpers.json import json_response
from ...models import ErrorCode

_LOGGER = logging.getLogger(__name__)

_UPLOAD_CHUNK_BYTES = 64 * 1024


def _reject(details: str, *, code: ErrorCode = ErrorCode.INVALID_ARGS) -> web.Response:
    return json_response({"error_code": str(code), "details": details}, status=400)


class UploadTokens(CapabilityTokens[bool]):
    """Capability tokens authorizing an HTTP bundle upload (bound to nothing)."""

    def create(self) -> str:
        # The payload is a bare presence sentinel — the token binds no resource.
        return self._mint(True)  # noqa: FBT003

    def consume(self, token: str) -> bool:
        """Pop a token; ``False`` if unknown, already-used, or expired."""
        return self._redeem(token) is not None


async def http_import_bundle(request: web.Request) -> web.StreamResponse:
    """``POST /api/devices/import_bundle?token=&mode=`` — import a bundle upload.

    HTTP (not WebSocket) so a large bundle isn't capped by a proxy's WebSocket
    ``max_msg_size``. The body streams straight in; ``mode=resolve`` with
    repeated ``overwrite`` params resolves conflicts, ``detect`` (or an absent
    mode) reports them. Returns an ``ImportBundleResponse`` as JSON.
    """
    db = request.app["device_builder"]
    if not db.devices.import_tokens.consume(request.query.get("token", "")):
        _LOGGER.debug("Bundle upload rejected: missing, unknown, reused, or expired token")
        raise web.HTTPNotFound
    # Reject an unknown mode (or overwrite params without mode=resolve) with a
    # clear 400 instead of silently falling back to detect and dropping the
    # caller's overwrite choices.
    mode = request.query.get("mode")
    if mode not in (None, "detect", "resolve"):
        return _reject(f"Unknown import mode {mode!r} (expected 'detect' or 'resolve').")
    overwrite_params = request.query.getall("overwrite", [])
    if mode == "resolve":
        overwrite: list[str] | None = overwrite_params
    elif overwrite_params:
        return _reject("overwrite params require mode=resolve.")
    else:
        overwrite = None
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
    try:
        result = await db.devices.import_bundle(bundle_bytes=buf, overwrite=overwrite)
    except CommandError as err:
        _LOGGER.warning("Bundle import rejected: %s", err.message)
        return _reject(err.message, code=err.code)
    return json_response(result)
