"""Offloader-side peer-link Noise WS client (one-shot + long-lived)."""

from __future__ import annotations

from .._client_models import (
    DownloadArtifactsError,
    DownloadArtifactsResult,
    DuplicateRequestError,
    InitiatorRoundTrip,
    PairStatusResult,
    PeerLinkClientError,
    PeerLinkNoSessionError,
    PeerLinkPinMismatchError,
    PreviewResult,
    RequestPairResult,
    SubmitJobSessionLostError,
    SubmitJobTimeoutError,
    _DownloadArtifactsState,
    _SessionLoopState,
)
from .client import PeerLinkClient
from .one_shot import (
    _build_ws_url,
    _drive_initiator_handshake_and_read_response,
    _extract_auto_provision_supported,
    _extract_ha_addon,
    _extract_receiver_esphome_version,
    _extract_receiver_friendly_name,
    _extract_reset_build_env_supported,
    await_pair_status,
    drive_initiator_round_trip,
    preview_pair,
    request_pair,
)

__all__ = (
    "DownloadArtifactsError",
    "DownloadArtifactsResult",
    "DuplicateRequestError",
    "InitiatorRoundTrip",
    "PairStatusResult",
    "PeerLinkClient",
    "PeerLinkClientError",
    "PeerLinkNoSessionError",
    "PeerLinkPinMismatchError",
    "PreviewResult",
    "RequestPairResult",
    "SubmitJobSessionLostError",
    "SubmitJobTimeoutError",
    "_DownloadArtifactsState",
    "_SessionLoopState",
    "_build_ws_url",
    "_drive_initiator_handshake_and_read_response",
    "_extract_auto_provision_supported",
    "_extract_ha_addon",
    "_extract_receiver_esphome_version",
    "_extract_receiver_friendly_name",
    "_extract_reset_build_env_supported",
    "await_pair_status",
    "drive_initiator_round_trip",
    "preview_pair",
    "request_pair",
)
