"""Remote-build feature models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from mashumaro.mixins.orjson import DataClassORJSONMixin


class RemoteBuildPeerSource(StrEnum):
    """
    How a peer dashboard ended up in :meth:`list_hosts`.

    ``mdns``: discovered via the ``_esphomebuilder._tcp.local.``
    browse. ``manual``: added by the user via
    ``remote_build/add_manual_host`` for cross-subnet or
    non-multicast LANs where mDNS doesn't reach but L3 unicast
    does.
    """

    MDNS = "mdns"
    MANUAL = "manual"


@dataclass
class ManualHost(DataClassORJSONMixin):
    """
    A user-supplied peer entry stored in the metadata sidecar.

    Persisted under ``_remote_build.manual_hosts``; merged into
    :meth:`list_hosts` output as a :class:`RemoteBuildPeer` row
    with ``source=MANUAL`` and empty version fields. Phase 2b does
    no version / fingerprint resolution; phase 4 attempts the
    connection and fills the version fields in.
    """

    hostname: str
    port: int


@dataclass
class StoredToken(DataClassORJSONMixin):
    """
    A receiver-side issued bearer token, persisted by hash.

    Cleartext is the wire form ``{token_id}.{secret}``; only
    ``secret_sha256`` lands on disk. ``token_id`` is the lookup key
    (constant-time table hit), ``secret_sha256`` is what the
    middleware compares against the bearer's secret half via
    ``hmac.compare_digest``.

    ``bound_dashboard_id`` starts ``None`` and is filled in by the
    phase-3b3 first-use binding the first time an authenticated
    request arrives carrying a peer's ``X-Dashboard-ID``. After
    that, requests presenting the same token but a different
    dashboard_id are rejected as 403.
    """

    token_id: str
    label: str
    secret_sha256: str
    created_at: float
    bound_dashboard_id: str | None = None


@dataclass
class TokenSummary(DataClassORJSONMixin):
    """
    Public-facing token row for ``remote_build/list_tokens``.

    Mirrors :class:`StoredToken` but drops ``secret_sha256``: the
    stored hash isn't sensitive in the same way the cleartext is,
    but exposing it would let a network attacker who's already
    seen the on-disk metadata match candidate cleartext bearers
    against the wire shape, so the frontend has no business
    reading it.
    """

    token_id: str
    label: str
    created_at: float
    bound_dashboard_id: str | None = None


@dataclass
class RemoteBuildSettings(DataClassORJSONMixin):
    """
    Receiver-side settings for the remote-build feature (storage shape).

    Stored in ``.device-builder.json`` under the ``_remote_build``
    top-level key. ``tokens`` carries :class:`StoredToken` rows
    *with* the ``secret_sha256`` hash; this is the on-disk /
    in-process shape only and MUST NOT be serialised over the
    wire. Use :class:`RemoteBuildSettingsView` (or the
    ``_summarise_token`` projection) for any response that leaves
    the server.
    """

    enabled: bool = False
    manual_hosts: list[ManualHost] = field(default_factory=list)
    tokens: list[StoredToken] = field(default_factory=list)


@dataclass
class RemoteBuildSettingsView(DataClassORJSONMixin):
    """
    Wire view of :class:`RemoteBuildSettings`.

    Returned from every WS command that exposes settings to a
    client. Identical to :class:`RemoteBuildSettings` except
    ``tokens`` is a list of :class:`TokenSummary` (no
    ``secret_sha256``), so issuing or removing tokens via the
    CRUD methods can't accidentally leak the stored hash back to
    the frontend through the response shape.
    """

    enabled: bool = False
    manual_hosts: list[ManualHost] = field(default_factory=list)
    tokens: list[TokenSummary] = field(default_factory=list)


@dataclass
class RemoteBuildPeer(DataClassORJSONMixin):
    """
    A peer dashboard known to this dashboard.

    Wire shape returned from ``remote_build/list_hosts``. Two
    sources land in the same row shape:

    * ``source=MDNS``: discovered via the
      ``_esphomebuilder._tcp.local.`` browse. ``name`` is the
      mDNS service-instance name (leftmost label, e.g.
      ``desktop``); ``hostname`` is the SRV target (e.g.
      ``desktop.local.``); ``addresses`` is the parsed A / AAAA
      list with IPv6 scope preserved; versions come from TXT.
    * ``source=MANUAL``: user-supplied via
      ``remote_build/add_manual_host``. ``name`` is the full
      hostname verbatim (NOT the leftmost label) so an IP-only
      entry like ``192.168.1.10`` reads sensibly in the UI rather
      than truncating to ``"192"``. ``hostname`` is the same
      user-entered string, ``port`` is the user-entered port,
      ``addresses`` is empty, and version fields are blank until
      phase 4 attempts the connection.

    Phase 2 stops at discovery + manual entry; pairing / connection
    / fingerprint pinning lands in later phases.
    """

    name: str
    hostname: str
    port: int
    source: RemoteBuildPeerSource
    addresses: list[str] = field(default_factory=list)
    server_version: str = ""
    esphome_version: str = ""


@dataclass
class IdentityView(DataClassORJSONMixin):
    """
    Receiver-side dashboard identity, projected for the Settings UI.

    Returned from ``remote_build/get_identity`` and
    ``remote_build/rotate_identity``. The cert + key PEMs are
    intentionally NOT included: only the ``pin_sha256`` (the
    SHA-256 of the cert's SubjectPublicKeyInfo, lowercase hex) is
    safe to ship, and the cert PEM itself adds nothing the
    fingerprint doesn't already let an offloader pin against.

    ``server_version`` is this dashboard's package version;
    ``esphome_version`` is the bundled esphome's. Both are also
    advertised in mDNS TXT (see :class:`DashboardAdvertiser`),
    but the Settings UI doesn't browse mDNS to render its own
    "Build host" card — surfacing them here keeps the card a
    single WS call.

    ``listener_bound`` reports whether the
    ``/remote-build/v1/*`` HTTPS receiver site is currently
    serving traffic on this dashboard. Lets the Settings UI
    distinguish "rotation succeeded AND the listener is back
    up" from "rotation succeeded but the rebuild fail-softed"
    (port now bound by something else, cert load throws, …).
    The latter is silent in the logs without this flag.
    """

    dashboard_id: str
    pin_sha256: str
    server_version: str
    esphome_version: str
    listener_bound: bool = False
