"""Remote-build feature models."""

from __future__ import annotations

from dataclasses import dataclass, field

from mashumaro.mixins.orjson import DataClassORJSONMixin


@dataclass
class RemoteBuildSettings(DataClassORJSONMixin):
    """
    Receiver-side settings for the remote-build feature.

    Stored in ``.device-builder.json`` under the ``_remote_build``
    top-level key. ``enabled`` is the master switch — phase 3 will
    gate ``/remote-build/v1/*`` route registration on it; phase 2
    just persists the flag so the Settings UI has somewhere to
    write.
    """

    enabled: bool = False


@dataclass
class RemoteBuildPeer(DataClassORJSONMixin):
    """
    A peer dashboard discovered via mDNS browse of ``_esphomebuilder._tcp.local.``.

    Wire shape returned from ``remote_build/list_hosts``. ``name`` is
    the mDNS service-instance name (the leftmost label, e.g.
    ``desktop``); ``hostname`` is the SRV target (e.g. ``desktop.local.``).
    Versions come from the TXT record. ``addresses`` is the parsed
    A / AAAA list — what an offloader would actually connect to.
    Phase 2 stops at discovery; pairing / connection / fingerprint
    pinning lands in later phases.
    """

    name: str
    hostname: str
    port: int
    addresses: list[str] = field(default_factory=list)
    server_version: str = ""
    esphome_version: str = ""
