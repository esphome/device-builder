"""Origin / Host header predicates shared by the WS handshake and CORS middleware."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse, urlsplit


def origin_matches_host(origin: str, request_host: str) -> bool:
    """Return True when ``origin``'s ``host:port`` matches the request's ``Host``."""
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    return bool(parsed.netloc) and parsed.netloc == request_host


def origin_in_allowlist(origin: str, allowlist: list[str]) -> bool:
    """Return True when ``origin``'s hostname is in ``allowlist`` (``"*"`` matches any)."""
    if not allowlist:
        return False
    if "*" in allowlist:
        return True
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    hostname = normalize_host(parsed.hostname or "")
    if not hostname:
        return False
    return any(normalize_host(entry) == hostname for entry in allowlist)


def host_in_allowlist(request_host: str, allowlist: list[str]) -> bool:
    """Return True when ``request_host`` is in ``allowlist``; empty list disables the check."""
    if not allowlist or "*" in allowlist:
        return True
    normalised = normalize_host(request_host)
    return any(normalize_host(entry) == normalised for entry in allowlist)


def normalize_host(host: str) -> str:
    """
    Lower-case ``host``, stripping the port and IPv6 brackets if any.

    IP literals are canonicalised through ``ipaddress`` so two
    spellings of one address (compressed ``2001:db8::1`` vs
    expanded ``2001:0db8:0:0:0:0:0:1``) normalise equal — an
    allowlist entry matches whatever spelling the Host header
    carries. Bare un-bracketed IPv6 needs the short-circuit
    because ``urlsplit`` would parse ``fe80`` as the host and
    ``:1`` as the port.
    """
    stripped = host.strip()
    if not stripped.startswith("["):
        try:
            return str(ipaddress.ip_address(stripped))
        except ValueError:
            pass
    try:
        hostname = urlsplit(f"//{stripped}").hostname
    except ValueError:
        hostname = None
    if hostname is None:
        return stripped.lower()
    try:
        return str(ipaddress.ip_address(hostname))
    except ValueError:
        return hostname.lower()


def request_origin_allowed(origin: str, request_host: str, trusted_domains: list[str]) -> bool:
    """Return True when ``origin`` is same-origin or its hostname is in ``trusted_domains``."""
    return origin_matches_host(origin, request_host) or origin_in_allowlist(origin, trusted_domains)
