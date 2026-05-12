"""
Controller-internal model types for the remote-build package.

These dataclasses + enum + dispatch table aren't on the wire —
they're internal value types shared between the controller's
methods and the helper modules that the controller composes
with. They're separated out from ``controller.py`` to keep the
class body focused on lifecycle + business logic and the
typed-value layer in one place.

* :class:`PeerLinkClientHandle` bundles a long-lived
  :class:`PeerLinkClient` with its run task so a single
  :attr:`~OffloaderController._peer_link_clients` lookup
  yields both, instead of two parallel dicts that could
  drift.
* :class:`RebindProbeOutcome` / :class:`RebindProbeResult` are
  the typed shape :meth:`_probe_pairing_endpoint` returns to
  its two callers (auto mDNS rebind + user-driven endpoint
  edit), so the surface mapping (silent log vs typed
  :class:`CommandError`) lives at each call site rather than
  inside the probe.
* :data:`EDIT_PAIRING_PROBE_ERRORS` is the dispatch table the
  WS-driven user path uses to collapse four near-identical
  ``raise`` blocks into one.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum

from ...models import ErrorCode
from .peer_link_client import PeerLinkClient, PeerLinkClientError


@dataclass(frozen=True)
class PeerLinkClientHandle:
    """Bundle a :class:`PeerLinkClient` with its run task.

    The client exposes the per-session API
    (:meth:`PeerLinkClient.submit_job`,
    :attr:`PeerLinkClient.is_session_open`); the task carries
    the cancellation handle the controller's lifecycle wiring
    needs (cancel on unpair, drain in :meth:`stop`). Held in
    :attr:`OffloaderController._peer_link_clients` so a single
    lookup yields both, instead of two parallel dicts that
    could drift.
    """

    client: PeerLinkClient
    task: asyncio.Task[None]


class RebindProbeOutcome(StrEnum):
    """Typed outcome of :meth:`OffloaderController._probe_pairing_endpoint`.

    The probe is shared between mDNS-driven auto-rebind and
    user-driven manual edit; each caller maps the outcome onto
    its own surface (silent log + cooldown for auto, typed
    :class:`CommandError` for the WS-driven user path). The
    enum factors out the four distinct probe failure modes so
    the surface mapping lives at the call site instead of in a
    per-caller bespoke probe body.
    """

    OK = "ok"
    UNREACHABLE = "unreachable"
    PIN_MISMATCH = "pin_mismatch"
    PAIRING_REPLACED = "pairing_replaced"
    STATUS_CHANGED = "status_changed"


@dataclass(frozen=True, slots=True)
class RebindProbeResult:
    """Result of :meth:`OffloaderController._probe_pairing_endpoint`.

    *observed_pin* is populated only on
    :attr:`RebindProbeOutcome.PIN_MISMATCH` (so the caller's
    error surface can name which identity answered at the
    candidate endpoint); *transport_error* is populated only
    on :attr:`RebindProbeOutcome.UNREACHABLE` (the
    :class:`PeerLinkClientError` instance, kept as the
    exception itself so the auto-rebind path's debug log can
    pass it as ``exc_info=`` to preserve the traceback while
    the user-driven path can ``str()`` it for the
    :class:`CommandError` message).
    """

    outcome: RebindProbeOutcome
    observed_pin: str = ""
    transport_error: PeerLinkClientError | None = None


# Dispatch table mapping a non-OK probe outcome to the typed
# :class:`CommandError` shape :meth:`edit_pairing_endpoint`
# raises for it. Each entry is ``(error_code, message_template)``;
# the template uses ``str.format`` with the keyword args
# ``host`` / ``port`` / ``pin`` / ``observed`` / ``error`` (all
# pre-formatted at call time so the templates stay declarative).
# Keeps the four probe-failure raise sites in
# :meth:`edit_pairing_endpoint` collapsed to one ``raise`` instead
# of four near-identical ``if … raise`` blocks.
EDIT_PAIRING_PROBE_ERRORS: dict[RebindProbeOutcome, tuple[ErrorCode, str]] = {
    RebindProbeOutcome.UNREACHABLE: (
        ErrorCode.UNAVAILABLE,
        "edit_pairing_endpoint: {host}:{port} unreachable: {error}",
    ),
    RebindProbeOutcome.PIN_MISMATCH: (
        # Different identity at the new coords. Leaves the
        # stored pairing untouched — the user's existing trust
        # is keyed on the original pin; substituting a fresh
        # pubkey under that trust is the case 8a's re-auth
        # wizard exists specifically to gate. The message
        # carries both observed and stored pin so the dialog
        # can render the "different identity at this endpoint"
        # copy and route the user to re-pair.
        ErrorCode.PRECONDITION_FAILED,
        "edit_pairing_endpoint: {host}:{port} answers with pin {observed!r}, not stored {pin!r}",
    ),
    RebindProbeOutcome.PAIRING_REPLACED: (
        ErrorCode.NOT_FOUND,
        "edit_pairing_endpoint: pairing for pin_sha256={pin!r} changed during probe; please retry",
    ),
    RebindProbeOutcome.STATUS_CHANGED: (
        ErrorCode.PRECONDITION_FAILED,
        "edit_pairing_endpoint: pairing status changed during probe",
    ),
}
