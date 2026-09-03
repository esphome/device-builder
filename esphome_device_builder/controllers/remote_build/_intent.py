"""Typed peer-link intent decision shared by the wire dispatch and controllers."""

from __future__ import annotations

from dataclasses import dataclass

from ...models import IntentResponse, RejectReason


@dataclass(frozen=True)
class IntentOutcome:
    """
    A receiver-side intent decision: the wire response plus an optional reason.

    ``reason`` rides the wire to disambiguate the opaque
    ``REJECTED`` (and marks a not-yet-approved ``PENDING`` on the
    lookup path); the self-describing ``OK`` / ``APPROVED`` /
    ``NO_PAIRING_WINDOW`` responses leave it ``None``.
    """

    response: IntentResponse
    reason: RejectReason | None = None
