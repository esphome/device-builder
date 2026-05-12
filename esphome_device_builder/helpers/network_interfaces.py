"""Resolve a NIC name into the IP addresses to bind to.

When ``--host`` (or ``--ingress-host`` / ``--remote-build-host``)
is given an interface name like ``eth0`` instead of an IP, we want
to bind the listener to every IPv4 / IPv6 address the kernel has
assigned to that interface. Useful inside Docker host-network mode
on a multi-homed host where the LAN IP isn't known in advance —
the operator points at the interface, the listener follows
whatever addresses it currently carries.

Inspired by esphome/esphome#15485, which solved the same problem
for the legacy Tornado dashboard.
"""

from __future__ import annotations

import socket

import psutil


def resolve_bind_host(host: str) -> list[str]:
    """
    Return the bind targets for *host* — verbatim, or the NIC's IPs.

    Raises :class:`OSError` when *host* names an interface with no bindable address.
    """
    addrs = psutil.net_if_addrs().get(host)
    if addrs is None:
        return [host]

    out: list[str] = []
    for addr in addrs:
        if addr.family == socket.AF_INET:
            out.append(addr.address)
        elif addr.family == socket.AF_INET6:
            address = addr.address
            # Link-local IPv6 (fe80::/10) is per-interface; the
            # kernel needs a zone identifier to disambiguate which
            # interface to send through. psutil sometimes already
            # carries one (``fe80::1%eth0``) on some platforms; skip
            # when present.
            if address.lower().startswith("fe80:") and "%" not in address:
                address = f"{address}%{socket.if_nametoindex(host)}"
            out.append(address)

    if not out:
        raise OSError(
            f"Network interface {host!r} has no bindable IPv4/IPv6 address; "
            "refusing to start. Bring the interface up, assign an address, "
            "or pass an IP literal instead."
        )

    return out
