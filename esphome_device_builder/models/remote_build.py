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
class RemoteBuildSettings(DataClassORJSONMixin):
    """
    Receiver-side settings for the remote-build feature.

    Stored in ``.device-builder.json`` under the ``_remote_build``
    top-level key. ``enabled`` is the master switch that phase 3
    will gate ``/remote-build/v1/*`` route registration on. Phase
    2 just persists the flag so the Settings UI has somewhere to
    write. ``manual_hosts`` is the user-supplied peer list (see
    :class:`ManualHost`).
    """

    enabled: bool = False
    manual_hosts: list[ManualHost] = field(default_factory=list)


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
