"""
WS-command argument validators + error mappers for the remote-build controller.

Every ``remote_build/*`` WS command rejects malformed input as
:class:`~helpers.api.CommandError(INVALID_ARGS)` at the boundary
rather than letting bad shapes leak into the controller's
business logic, the on-disk stores, or the peer-link wire layer.
Each validator owns one input shape (hostname / port /
pin_sha256 / dashboard_id / pair-flow label / submit-job target
/ strict-bool) and returns the cleaned form the controller
caches against.

Companion error-mapping helpers translate non-success outcomes
on the peer-link wire (``IntentResponse.REJECTED`` /
``NO_PAIRING_WINDOW``, the receiver's
``artifacts_end{accepted: false, reason}`` reject reasons) into
the matching :class:`CommandError` shape so the WS layer
surfaces a single typed error rather than wire strings the
frontend would have to parse.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, cast

from yarl import URL

from ...helpers.api import CommandError
from ...helpers.dashboard_identity import DASHBOARD_ID_MAX_CHARS, DASHBOARD_ID_PATTERN
from ...models import ErrorCode, IntentResponse
from .peer_link_client import DownloadArtifactsError

# RFC 1035 §2.3.4 caps a fully-qualified domain name at 253
# characters; round up to 255 to leave room for trailing-dot
# variations. The cap stops a misbehaving frontend from bloating
# the on-disk pairings file with a megabyte-string masquerading
# as a hostname.
_HOSTNAME_MAX_CHARS = 255

# 32-byte SHA-256 → 64 lowercase-hex chars.
_PIN_SHA256_LEN = 64

# Matches the receiver-side ``_PEER_LABEL_MAX_CHARS`` truncation
# cap so a label that round-trips through ``pair_request`` lands
# identically on both sides.
_PAIR_LABEL_MAX_CHARS = 128


class HostFieldContext(StrEnum):
    """Error-message prefix for the shared host / port validators.

    The same :func:`validate_hostname` / :func:`validate_port`
    pair gates the offloader-side ``preview_pair`` /
    ``request_pair`` flow and any future receiver-side
    host-input surface. Hardcoding a single prefix in the error
    messages would leak misleading diagnostics into the WS
    layer; pick the right prefix at the call site instead.

    StrEnum values are the message prefix verbatim; new call
    sites that want a distinct user-facing string add a new
    enum member rather than passing a free-form string (so the
    prefixes are grep-able and don't drift).
    """

    RECEIVER = "receiver"


class PairLabelField(StrEnum):
    """Wire arg name for :func:`validate_pair_label` error messages.

    ``request_pair`` takes two distinct labels —
    ``receiver_label`` for local storage and ``offloader_label``
    sent to the receiver in msg3 — and a validation failure
    must name the failing arg so the frontend can pin the
    inline error to the right input. StrEnum values are the
    wire arg name verbatim (mirrors :class:`HostFieldContext`).
    """

    RECEIVER_LABEL = "receiver_label"
    OFFLOADER_LABEL = "offloader_label"


# Allowed values of ``submit_job``'s ``target`` arg. Wire-side
# the receiver enforces the same set
# (:data:`controllers.remote_build.submit_job._TARGET_TO_JOB_TYPE`);
# rejecting unknown targets here means a typo lands as a clean
# ``INVALID_ARGS`` for the frontend to render inline rather than
# a ``submit_job_ack{accepted: false, reason: "invalid_header"}``
# only after the bundle's been built and shipped.
_SUBMIT_JOB_VALID_TARGETS: frozenset[str] = frozenset({"compile", "upload"})


# Maps non-success ``IntentResponse`` values from a peer-link
# round-trip to the typed :class:`CommandError` the frontend
# branches on. Used by ``request_pair`` to surface the
# receiver's decision (the offloader-side pair-status listener
# task handles its own ``IntentResponse`` branches inline rather
# than going through CommandError, so this map only covers the
# WS-command request_pair path).
_INTENT_RESPONSE_ERRORS: dict[IntentResponse, tuple[ErrorCode, str]] = {
    IntentResponse.NO_PAIRING_WINDOW: (
        ErrorCode.NO_PAIRING_WINDOW,
        "receiver pairing window closed; ask the receiver-side admin to "
        "open Settings → Build server → Pairing requests, then retry",
    ),
    IntentResponse.REJECTED: (
        ErrorCode.PRECONDITION_FAILED,
        "receiver declined the pair request",
    ),
}


# Mapping from receiver-reported reject reasons (carried on
# ``artifacts_end{accepted: false}``) to the
# :class:`ErrorCode` the WS layer surfaces. Receiver-side
# constants live in :mod:`controllers.remote_build.artifacts_download`;
# the wire string is the canonical seam, not a shared enum,
# because it crosses the trust boundary as user-controlled
# JSON. Unknown reasons fall through to ``UNAVAILABLE`` (better
# safe than asserting on a stranger's wire format).
_DOWNLOAD_ARTIFACTS_REASON_TO_ERROR_CODE: dict[str, ErrorCode] = {
    "unknown_job": ErrorCode.NOT_FOUND,
    "build_dir_missing": ErrorCode.NOT_FOUND,
    "job_not_completed": ErrorCode.PRECONDITION_FAILED,
    "duplicate_download": ErrorCode.PRECONDITION_FAILED,
    "pack_failed": ErrorCode.UNAVAILABLE,
}


def validate_hostname(raw: object, *, context: HostFieldContext = HostFieldContext.RECEIVER) -> str:
    """
    Normalise a user-entered hostname to its canonical lowercase form.

    Rejects non-string and empty / whitespace-only input with
    :class:`CommandError(INVALID_ARGS)`. Caps length at
    :data:`_HOSTNAME_MAX_CHARS` (RFC 1035 §2.3.4 caps an FQDN at
    253; we accept up to 255 to leave room for trailing-dot
    variations).

    Defers the URL-validity check to :class:`yarl.URL.build` so
    the WS-command validator and the offloader's
    ``_build_ws_url`` (in
    :mod:`controllers.remote_build.peer_link_client`) share a
    single source of truth on what constitutes a host. yarl
    rejects ``/``, ``?``, ``#``, ``@``, embedded ``:port``, and
    other URL-injection shapes that would otherwise let a
    pathological hostname smuggle path / query / fragment /
    userinfo into the rendered URL.

    Lowercase normalisation matches the duplicate-check
    semantics; hostnames are case-insensitive per RFC 1035
    §2.3.3, so ``Desktop.local`` and ``desktop.local`` collapse
    to one entry. The actual connection runs when pairing
    attempts it (and discovers DNS / TLS validity); we
    deliberately don't pre-flight an "is this resolvable now?"
    check, which would fail on offline laptops adding a peer
    for later.
    """
    if not isinstance(raw, str):
        msg = f"{context}: 'hostname' must be a string"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    trimmed = raw.strip().lower()
    if not trimmed:
        msg = f"{context}: 'hostname' must not be empty"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    if len(trimmed) > _HOSTNAME_MAX_CHARS:
        msg = f"{context}: 'hostname' must be at most {_HOSTNAME_MAX_CHARS} characters"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    # The ``port=80, path="/"`` are sentinels for the build call
    # — only the host arg is being validated. yarl's host parser
    # is the same one ``_build_ws_url`` will use later, so any
    # input that passes here is guaranteed to round-trip
    # through the URL builder without raising.
    try:
        URL.build(scheme="ws", host=trimmed, port=80, path="/")
    except ValueError as exc:
        msg = f"{context}: 'hostname' is not a valid host: {exc}"
        raise CommandError(ErrorCode.INVALID_ARGS, msg) from exc
    return trimmed


def validate_port(raw: object, *, context: HostFieldContext = HostFieldContext.RECEIVER) -> int:
    """
    Validate a user-entered port number.

    ``bool`` is rejected even though ``isinstance(True, int)``
    is true; accepting ``True`` for a port number is a footgun
    (silently coerces to 1, which IANA reserves for tcpmux).
    Range is the IANA-registered ephemeral plus well-known:
    1-65535.

    *context* prefixes every error message; see
    :class:`HostFieldContext` for the rationale.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        msg = f"{context}: 'port' must be an integer"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    if not 1 <= raw <= 65535:
        msg = f"{context}: 'port' must be between 1 and 65535"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    return raw


def validate_pin_sha256(raw: object) -> str:
    """Validate a wire ``pin_sha256`` value as 64 lowercase-hex chars.

    Same alphabet and length the storage seam enforces in
    :class:`StoredPairing` (and the receiver's
    :class:`StoredPeer`), just at the WS-command boundary so a
    bad pin gets rejected as ``INVALID_ARGS`` before the
    offloader opens a Noise WS only to fail the TOCTOU check
    post-handshake.
    """
    if not isinstance(raw, str):
        msg = "pin_sha256 must be a string"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    cleaned = raw.strip()
    if (
        len(cleaned) != _PIN_SHA256_LEN
        or not cleaned.isascii()
        or any(c not in "0123456789abcdef" for c in cleaned)
    ):
        msg = f"pin_sha256 must be {_PIN_SHA256_LEN} lowercase-hex characters"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    return cleaned


def validate_pair_label(raw: object, *, field: PairLabelField) -> str:
    """Validate a user-supplied pair-flow label.

    Capped at 128 chars to match the receiver's truncation cap
    on the same field, so a label that round-trips through
    ``pair_request`` lands in both sides' tables with identical
    content. Empty labels are allowed; the user may
    legitimately not name the receiver yet, and the frontend
    can render a placeholder.

    Rejects strings containing C0 / C1 control chars (incl.
    null bytes, ANSI escapes, newlines, DEL) via
    :meth:`str.isprintable`. The ``offloader_label`` transits
    to the receiver-side admin UI's pairing-requests inbox;
    refusing control chars here is defense-in-depth against
    ANSI / bidi-override / null-byte injection attacks against
    an admin terminal or log reader. Non-ASCII printables
    (CJK, accented Latin, emoji) pass.

    *field* names the failing arg in the diagnostic via
    :class:`PairLabelField` so the frontend can pin the inline
    error to the right input.
    """
    if not isinstance(raw, str):
        msg = f"{field} must be a string"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    cleaned = raw.strip()
    if len(cleaned) > _PAIR_LABEL_MAX_CHARS:
        msg = f"{field} must be at most {_PAIR_LABEL_MAX_CHARS} characters"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    if not cleaned.isprintable():
        msg = f"{field} must contain only printable characters"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    return cleaned


def validate_submit_job_target(raw: object) -> Literal["compile", "upload"]:
    """Validate the WS *target* arg for ``remote_build/submit_job``."""
    if not isinstance(raw, str) or raw not in _SUBMIT_JOB_VALID_TARGETS:
        msg = f"target must be one of {sorted(_SUBMIT_JOB_VALID_TARGETS)}; got {raw!r}"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    return cast(Literal["compile", "upload"], raw)


def validate_bool(raw: object, *, command: str, field: str) -> bool:
    """Strict-bool validation for a WS-command argument.

    Rejects non-``bool`` values rather than coercing — a string
    ``"false"`` is truthy under ``bool()`` and would persist the
    opposite of the operator's intent on a security-relevant
    switch. The diagnostic names *command* and *field* so the
    frontend can pin the inline error to the right input.
    """
    if not isinstance(raw, bool):
        msg = f"{command}: {field!r} must be a boolean"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    return raw


def validate_dashboard_id(raw: object) -> str:
    """
    Validate a user-supplied ``dashboard_id`` argument.

    Same alphabet and length cap the peer-link Noise dispatcher
    enforces on the msg3-supplied ``dashboard_id``; the regex +
    max-length live in :mod:`helpers.dashboard_identity` so the
    WS-command path here and the Noise-frame path can't drift
    apart.

    Rejects non-string / empty / oversized / non-base64url
    input with ``INVALID_ARGS`` rather than silently looking up
    nothing (which would yield a misleading ``NOT_FOUND``).
    """
    if not isinstance(raw, str):
        msg = "dashboard_id must be a string"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    cleaned = raw.strip()
    if (
        not cleaned
        or len(cleaned) > DASHBOARD_ID_MAX_CHARS
        or not DASHBOARD_ID_PATTERN.fullmatch(cleaned)
    ):
        msg = f"dashboard_id must be 1-{DASHBOARD_ID_MAX_CHARS} base64url chars"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    return cleaned


def intent_response_to_command_error(status: IntentResponse) -> CommandError | None:
    """Translate a non-success ``IntentResponse`` to a typed ``CommandError``.

    Returns ``None`` for the success values (``OK``, ``PENDING``,
    ``APPROVED``); the caller branches on those for persistence.
    Returns a fresh ``CommandError`` (not yet raised) for
    ``REJECTED`` / ``NO_PAIRING_WINDOW`` so the caller can
    decide whether to attach extra context before raising.
    """
    pair = _INTENT_RESPONSE_ERRORS.get(status)
    if pair is None:
        return None
    code, msg = pair
    return CommandError(code, msg)


def enforce_pin_match(*, expected: str, observed: str) -> None:
    """Raise ``CommandError(PRECONDITION_FAILED)`` on a TOCTOU pin drift.

    The offloader's ``request_pair`` (and any future pin-pinned
    re-handshake) compares the pin the user OOB-confirmed
    during ``preview_pair`` against the actual pubkey from the
    live handshake. A mismatch means the receiver rotated
    identity (or a MITM intervened) between preview and
    request; the offloader bails before persisting the row so a
    fresh preview round-trip is required.

    The error message carries both pins in full (no truncation)
    so the user can do a side-by-side OOB comparison against
    the receiver's "Build server" Settings card and tell which
    end's pin changed.
    """
    # Plain ``==`` is fine here: the pin is a SHA-256 of a
    # public key, broadcast in mDNS and rendered in the
    # receiver's UI. There's no secret to leak via timing;
    # constant-time comparison would be defending nothing.
    if expected == observed:
        return
    msg = f"receiver pin changed since preview; expected {expected}, got {observed}"
    raise CommandError(ErrorCode.PRECONDITION_FAILED, msg)


def download_artifacts_error_to_command_error(exc: DownloadArtifactsError) -> CommandError:
    """Map the receiver's structured reject reason to a typed :class:`CommandError`.

    Used by the ``remote_build/download_artifacts`` WS command
    after :meth:`PeerLinkClient.download_artifacts` raises. The
    error's ``reason`` attribute carries the receiver's
    ``artifacts_end{accepted: false, reason}`` value verbatim;
    this helper translates it into the matching
    :class:`ErrorCode` and forwards the human-readable message.
    Unknown reasons map to ``UNAVAILABLE`` — same shape as a
    transient transport failure, since "the receiver sent a
    reason we don't recognise" is most likely a version-skew
    issue we want to surface as retry-eligible rather than a
    permanent precondition failure.
    """
    code = _DOWNLOAD_ARTIFACTS_REASON_TO_ERROR_CODE.get(exc.reason, ErrorCode.UNAVAILABLE)
    return CommandError(code, str(exc))
