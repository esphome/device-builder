"""
mDNS TXT decoding + service-info projection for the remote-build controller.

The dashboard's ``_esphomebuilder._tcp.local.`` browse path
turns each :class:`zeroconf.asyncio.AsyncServiceInfo` from a
neighbouring dashboard into a :class:`RemoteBuildPeer` row for
the discovery list, and reads TXT values (``server_version``,
``esphome_version``, ``pin_sha256``, ``remote_build_port``)
defensively — a corrupted / missing / over-range TXT entry must
not raise into the browser callback or trigger spurious rebind
probes.

Two endpoints also compare ``(host, port)`` tuples
case-and-trailing-dot-insensitively (the dashboard's own
advertise lookup, and the mDNS-driven auto-rebind path), so
that lives here too.
"""

from __future__ import annotations

from zeroconf import IPVersion
from zeroconf.asyncio import AsyncServiceInfo

from ...helpers.hostname import normalize_hostname
from ...models import RemoteBuildPeer, RemoteBuildPeerSource


def decode_txt_value(raw: bytes | None) -> str:
    """Decode a TXT value as UTF-8, falling back to the empty string."""
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def decode_txt_port(raw: bytes | None) -> int:
    """Decode a TCP port from a TXT value; fall back to 0 on absent / malformed / out-of-range.

    Defensive: a corrupted / non-numeric / out-of-range TXT
    entry shouldn't raise into the browser callback (which
    would abort the resolve task and silently lose the peer),
    and shouldn't trigger spurious rebind probes for a port we
    couldn't dial anyway. Negative integers and values outside
    the IANA ``1..65535`` range collapse to the same "not
    advertised" 0 sentinel TXT-absent rows already produce, so
    downstream sites that gate on a real port cover absence,
    malformation, and spoof attempts in one check.
    """
    decoded = decode_txt_value(raw)
    if not decoded:
        return 0
    try:
        port = int(decoded)
    except ValueError:
        return 0
    if not 1 <= port <= 65535:
        return 0
    return port


def endpoints_equal(host_a: str, port_a: int, host_b: str, port_b: int) -> bool:
    """Case- and trailing-dot-insensitive equality on ``(host, port)`` pairs.

    Two paths in the controller compare an mDNS-resolved
    endpoint against another stored endpoint (the dashboard's
    own advertise via ``_is_self_endpoint``, and a stored
    pairing's coordinates via ``_maybe_schedule_rebind_probe``).
    Both want hostname comparison through
    :func:`helpers.hostname.normalize_hostname` so trailing-dot
    / case differences between the SRV target form and the
    stored form don't show up as spurious mismatches; this
    helper centralises the shape so the two call sites stay in
    lockstep on the normalisation rules.
    """
    return port_a == port_b and normalize_hostname(host_a) == normalize_hostname(host_b)


def peer_from_service_info(name: str, info: AsyncServiceInfo) -> RemoteBuildPeer:
    """
    Build a :class:`RemoteBuildPeer` from a resolved ``AsyncServiceInfo``.

    Keeps the parsing in one place so the cache-hit and the
    network-resolve branches produce identical shapes.

    Uses ``parsed_scoped_addresses(IPVersion.All)`` rather than
    ``parsed_addresses()`` so IPv6 link-local entries keep
    their ``%<interface>`` scope suffix. Without the scope, an
    ``fe80::xxx`` address parses but isn't connectable; the OS
    needs to know which interface to send the packet out on.
    Mirrors the choice made in :class:`DeviceStateMonitor`.
    """
    properties = info.properties or {}
    server_version = decode_txt_value(properties.get(b"server_version"))
    esphome_version = decode_txt_value(properties.get(b"esphome_version"))
    # ``pin_sha256`` and ``remote_build_port`` are emitted by
    # the advertiser only when the peer-link listener is bound
    # (see :class:`DashboardAdvertiser`). The offloader-side
    # mDNS auto-rebind path reads both: pin to match a
    # broadcast against a stored pairing, port to dial the
    # peer-link Noise WS on the new endpoint. ``""`` / ``0``
    # for receivers that haven't published them; the rebind
    # path silently skips those rows.
    pin_sha256 = decode_txt_value(properties.get(b"pin_sha256"))
    remote_build_port = decode_txt_port(properties.get(b"remote_build_port"))
    # ``info.name`` comes back as ``<instance>.<service_type>``;
    # we only want the leftmost label as the friendly name.
    instance = (info.name or name).split(".", 1)[0]
    server = info.server or ""
    return RemoteBuildPeer(
        name=instance,
        hostname=server,
        port=info.port or 0,
        source=RemoteBuildPeerSource.MDNS,
        addresses=info.parsed_scoped_addresses(IPVersion.All) or [],
        server_version=server_version,
        esphome_version=esphome_version,
        pin_sha256=pin_sha256,
        remote_build_port=remote_build_port,
    )
