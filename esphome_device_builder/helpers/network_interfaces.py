"""Resolve a NIC name into the IP addresses to bind to.

When ``--host`` (or ``--ingress-host`` / ``--remote-build-host``)
is given an interface name like 'eth0' instead of an IP, we want
to bind the listener to every IPv4 / IPv6 address the kernel has
assigned to that interface. Useful inside Docker host-network mode
on a multi-homed host where the LAN IP isn't known in advance;
the operator points at the interface, the listener follows
whatever addresses it currently carries.

Inspired by esphome/esphome#15485, which solved the same problem
for the legacy Tornado dashboard.

The resolution is one-shot at startup. ``ifaddr.get_adapters``
is blocking I/O (reads /proc/net on Linux, calls
``GetAdaptersAddresses`` on Windows), but callers run it
synchronously off the event loop in startup paths only; a
one-shot syscall scan during bind is acceptable. Any new async
caller that hits a hot path should route through
``loop.run_in_executor`` to match the convention in
``dashboard_advertise.py``.
"""

from __future__ import annotations

import errno
import socket
import sys

import ifaddr

# Windows raises WSAEACCES (mapped to EACCES) for Hyper-V/WinNAT
# excluded port ranges — functionally "port unavailable, try the
# next". POSIX EACCES means a privileged port: falling forward would
# mask a permission misconfiguration, so it propagates instead.
_PORT_UNAVAILABLE_ERRNOS = frozenset(
    {errno.EADDRINUSE, errno.EACCES} if sys.platform == "win32" else {errno.EADDRINUSE}
)

# Windows SO_REUSEADDR lets a second bind on an actively-listened port
# silently succeed; SO_EXCLUSIVEADDRUSE surfaces the conflict while
# still allowing binds over TIME_WAIT remnants. POSIX SO_REUSEADDR
# keeps a just-torn-down listener's TIME_WAIT remnant from reading as
# "taken". SO_EXCLUSIVEADDRUSE only exists on Windows; ``getattr``
# with the POSIX fallback doubles as the platform switch.
_BIND_SOCKOPT: int = getattr(socket, "SO_EXCLUSIVEADDRUSE", socket.SO_REUSEADDR)


def resolve_bind_host(host: str) -> list[str]:
    """
    Return the bind targets for *host* (verbatim, or the NIC's IPs).

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


def bind_available_port(
    hosts: list[str], start_port: int, attempts: int
) -> tuple[int, list[socket.socket]]:
    """
    Bind the first port >= *start_port* free on every host in *hosts*.

    Returns the actually-bound port (OS-assigned when *start_port* is 0) and
    the bound (not yet listening) sockets — holding them is what makes the
    reservation race-free against other instances scanning the same range;
    hand them to ``web.SockSite`` or close them. Scans at most *attempts*
    candidates; raises :class:`OSError` (``EADDRINUSE``) when the scan
    exhausts. Blocking I/O — call off the event loop.
    """
    if attempts < 1:
        raise ValueError(f"attempts must be >= 1, got {attempts}")
    if not 0 <= start_port <= 65535:
        raise ValueError(f"start_port must be 0-65535, got {start_port}")
    end = min(start_port + attempts, 65536)
    for candidate in range(start_port, end):
        sockets: list[socket.socket] = []
        try:
            for host in hosts:
                sockets.extend(_bind_sockets(host, candidate))
        except OSError as err:
            for sock in sockets:
                sock.close()
            if err.errno in _PORT_UNAVAILABLE_ERRNOS:
                continue
            raise
        return sockets[0].getsockname()[1], sockets
    raise OSError(
        errno.EADDRINUSE,
        f"No free port in {start_port}-{end - 1} on {hosts!r}; "
        "other dashboard instances may hold them all",
    )


def ensure_single_host_for_ephemeral_port(hosts: list[str], port: int, flag: str) -> None:
    """
    Refuse ephemeral-port (port=0) bind when *hosts* expands to more than one address.

    Each ``TCPSite(port=0)`` gets its own OS-assigned port, but
    callers that advertise the bound port elsewhere (e.g. the
    mDNS ``remote_build_port`` TXT) only carry one. Reusing the
    first site's port for subsequent binds isn't safe either,
    the OS doesn't guarantee it's free on the second adapter.
    """
    if port == 0 and len(hosts) > 1:
        raise RuntimeError(
            f"Refusing to bind: {flag} 0 (ephemeral) is incompatible "
            f"with the host argument resolving to multiple addresses "
            f"({hosts!r}). Pick a fixed port, or pass a single IP literal."
        )


def _bind_sockets(host: str, port: int) -> list[socket.socket]:
    """Bind a socket on every address of *host*:*port* and return them; OSError when taken."""
    sockets: list[socket.socket] = []
    try:
        for family, type_, proto, _canonname, sockaddr in socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
        ):
            sock = socket.socket(family, type_, proto)
            sockets.append(sock)
            sock.setsockopt(socket.SOL_SOCKET, _BIND_SOCKOPT, 1)
            if family == socket.AF_INET6 and hasattr(socket, "IPPROTO_IPV6"):
                # Mirror asyncio's create_server: each v6 socket binds
                # v6-only so a sibling v4 bind of the same port works.
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            sock.bind(sockaddr)
    except OSError:
        for sock in sockets:
            sock.close()
        raise
    return sockets
