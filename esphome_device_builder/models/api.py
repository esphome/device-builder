"""WebSocket API message models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .common import DashboardModel


class ErrorCode(StrEnum):
    """WebSocket API error codes."""

    INVALID_MESSAGE = "invalid_message"
    UNKNOWN_COMMAND = "unknown_command"
    INVALID_ARGS = "invalid_args"
    NOT_FOUND = "not_found"
    ALREADY_EXISTS = "already_exists"
    INTERNAL_ERROR = "internal_error"
    NOT_AUTHENTICATED = "not_authenticated"
    RATE_LIMITED = "rate_limited"
    # Transient external dependency failed — distinct from
    # ``INTERNAL_ERROR`` (backend bug) and ``INVALID_ARGS`` (user
    # typo). The frontend renders this as a "couldn't reach the
    # receiver / try again" toast rather than a stack-trace
    # diagnostic. Used by the offloader-side peer-link commands
    # (``preview_pair`` / ``request_pair``) when the remote
    # dashboard isn't reachable, the Noise handshake fails to
    # authenticate, or the post-handshake frame doesn't decrypt.
    UNAVAILABLE = "unavailable"
    # State precondition not met — the operation is well-formed
    # and the remote is reachable, but the current state of one
    # side disqualifies the request. Used by the offloader's
    # ``request_pair`` for: (a) pin mismatch between the value
    # the user OOB-confirmed in ``preview_pair`` and the actual
    # pubkey from the live handshake (TOCTOU defense — the
    # receiver may have rotated identity, or there's an active
    # MITM), and (b) the receiver returning ``rejected`` (admin
    # explicitly declined or there's a stale "rejected" memo
    # within the soft-block window). The frontend rendering
    # distinguishes these via the ``details`` field; both share
    # the same "you can't proceed past this without out-of-band
    # action" semantic.
    PRECONDITION_FAILED = "precondition_failed"
    # Pairing window on the receiver is closed — the offloader's
    # ``intent="pair_request"`` arrived outside the receiver's
    # admin-supervised acceptance window. The frontend prompts
    # the user to ask the receiver-side admin to open the
    # Pairing requests screen, then retry. Distinct from
    # ``UNAVAILABLE`` (transport failure: receiver unreachable)
    # and ``PRECONDITION_FAILED`` (receiver reachable + made a
    # decision); this is "receiver reachable but not currently
    # listening."
    NO_PAIRING_WINDOW = "no_pairing_window"
    # The offloader's strictest version-match policy
    # (``exact_required``) filtered out every paired peer and
    # there's no remote target to compile against. Distinct from
    # the other three policies, which silently fall back to a
    # LOCAL build when filtering empties the peer set — this
    # code is the operator-visible signal that "use my remote
    # build server" is incompatible with the current peer fleet,
    # so the install refuses rather than masking the policy
    # violation with a slow LOCAL compile.
    NO_COMPATIBLE_PEER = "no_compatible_peer"


@dataclass
class CommandMessage(DashboardModel):
    """Client -> Server: a command request."""

    command: str
    message_id: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class ResultMessage(DashboardModel):
    """Server -> Client: successful command result."""

    message_id: str
    result: Any = None


@dataclass
class ErrorMessage(DashboardModel):
    """Server -> Client: command error."""

    message_id: str
    error_code: ErrorCode
    details: str = ""


@dataclass
class EventMessage(DashboardModel):
    """Server -> Client: streaming output or push event."""

    message_id: str
    event: str
    data: Any = None


@dataclass
class ServerInfoMessage(DashboardModel):
    """Server -> Client: sent on connection."""

    server_version: str
    esphome_version: str
    port: int
    ha_addon: bool = False
    # True only when the connection is proxied through HA ingress; an
    # add-on reached directly on its exposed port is False, unlike ha_addon.
    ha_ingress: bool = False
    requires_auth: bool = False
    # ESPHome Desktop wrapper version, from ESPHOME_DESKTOP_VERSION; "" off-desktop.
    desktop_version: str = ""
    # True when the desktop app (0.14.0+) exposes its update `api` via
    # ESPHOME_DESKTOP_BIN; gates the frontend's "Check for updates" menu item.
    desktop_update_capable: bool = False
