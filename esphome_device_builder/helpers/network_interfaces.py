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

import ifaddr


def resolve_bind_host(host: str) -> list[str]:
    """
    Return the bind targets for *host* — verbatim, or the NIC's IPs.

    Raises :class:`OSError` when *host* names an interface with no bindable address.
    """
    adapter = next(
        (a for a in ifaddr.get_adapters() if host in (a.name, a.nice_name)),
        None,
    )
    if adapter is None:
        return [host]

    out: list[str] = []
    for ip in adapter.ips:
        match ip.ip:
            case str() as address:
                out.append(address)
            case (address, _flowinfo, scope_id):
                if scope_id:
                    address = f"{address}%{scope_id}"
                out.append(address)

    if not out:
        raise OSError(
            f"Network interface {host!r} has no bindable IPv4/IPv6 address; "
            "refusing to start. Bring the interface up, assign an address, "
            "or pass an IP literal instead."
        )

    return out
