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
class TokenCreateResult(DataClassORJSONMixin):
    """
    Response from ``remote_build/add_token``.

    The cleartext ``bearer`` flashes through this response exactly
    once at creation time; subsequent ``list_tokens`` calls return
    :class:`TokenSummary` rows that never carry the secret. The
    frontend is expected to show ``bearer`` to the user with a
    copy-to-clipboard control and stop displaying it once the
    dialog is dismissed.
    """

    token_id: str
    label: str
    created_at: float
    bearer: str


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
