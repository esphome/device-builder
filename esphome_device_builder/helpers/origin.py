"""Origin / Host header predicates shared by the WS handshake gate and CORS middleware.

The WS handshake (``api/ws.py``) and the REST CORS middleware
(``helpers/json.py``) both decide whether to accept a cross-origin
browser request — they share these helpers so a tightening on one
gate doesn't drift from the other. The functions are pure: input
is the raw header strings + the operator-supplied
``--trusted-domains`` allowlist, output is a bool.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse, urlsplit


def origin_matches_host(origin: str, request_host: str) -> bool:
    """Return True when *origin*'s host:port matches the request's Host header."""
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    return bool(parsed.netloc) and parsed.netloc == request_host


def origin_in_allowlist(origin: str, allowlist: list[str]) -> bool:
    """
    Return True when ``origin``'s hostname is in the allowlist.

    Used by the cross-origin acceptance gate: reverse-proxy
    deployments where Origin is ``https://dashboard.example.com``
    but Host is ``localhost:6052`` (proxy upstream) need the
    operator-supplied ``ESPHOME_TRUSTED_DOMAINS`` allowlist to
    accept the cross-origin handshake.

    The allowlist match is on the Origin URL's hostname (port and
    scheme stripped), case-insensitive. A bare hostname entry like
    ``dashboard.example.com`` matches an Origin of
    ``https://Dashboard.Example.com`` regardless of port; an entry
    of ``[::1]`` matches ``http://[::1]:6052``.

    ``"*"`` matches anything (escape hatch for operators who set
    the env var without a specific host list).
    """
    if not allowlist:
        return False
    if "*" in allowlist:
        return True
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return False
    return any(normalize_host(entry) == hostname for entry in allowlist)


def host_in_allowlist(request_host: str, allowlist: list[str]) -> bool:
    """
    Return True when ``request_host`` is permitted by ``allowlist``.

    ``allowlist`` is the operator-supplied ``--trusted-domains`` /
    ``$ESPHOME_TRUSTED_DOMAINS`` list — empty means "no allowlist,
    anything goes" and the caller skips the check entirely.

    Both ``request_host`` and each allowlist entry go through
    ``normalize_host`` (lower-case, port stripped, IPv6 brackets
    stripped). ``DashboardSettings.parse_args`` strips whitespace
    and lower-cases the entries on load but does NOT canonicalise
    bracket / port shape, so an entry of ``[::1]`` and a Host
    header of ``[::1]:6052`` (or an un-bracketed ``::1``) all
    end up normalised to ``::1`` here and compare equal.

    The literal ``"*"`` is an explicit "match anything" escape hatch
    for operators who want to record the config knob is set without
    restricting hosts (handy for split-hostname proxy setups where
    the Host header varies per request and the existing Origin/Host
    equality + auth chain is doing the work).

    Defense in depth on top of the existing Origin/Host equality
    check + per-IP-rate-limited ``auth/login``.
    """
    if not allowlist:
        return True
    if "*" in allowlist:
        return True
    normalised = normalize_host(request_host)
    return any(normalize_host(entry) == normalised for entry in allowlist)


def normalize_host(host: str) -> str:
    """
    Lower-case ``host`` and strip the port + IPv6 brackets, if any.

    HTTP ``Host`` headers carry IPv6 addresses bracket-wrapped
    (``[::1]:6052``); naive ``split(":", 1)`` would chop the first
    segment of the address. ``urlsplit("//" + host).hostname``
    handles both shapes (IPv4 / hostname:port and ``[ipv6]:port``)
    and returns the unbracketed lowercase hostname.

    There's one edge case ``urlsplit`` mis-handles: a bare IPv6
    address typed *without* brackets (operator's allowlist entry
    of ``fe80::1`` rather than ``[fe80::1]``) — ``urlsplit``
    parses the leading ``fe80`` as the host and ``:1`` as the
    port. Short-circuit those via ``ipaddress.ip_address`` before
    falling through to the URL-parser branch. Bracketed Host
    headers go straight to ``urlsplit`` which handles them
    correctly. Falls back to the input verbatim when ``urlsplit``
    returns nothing usable (malformed Host header).
    """
    stripped = host.strip()
    if not stripped.startswith("["):
        try:
            ipaddress.ip_address(stripped)
        except ValueError:
            pass
        else:
            return stripped.lower()
    try:
        hostname = urlsplit(f"//{stripped}").hostname
    except ValueError:
        hostname = None
    if hostname is None:
        return stripped.lower()
    return hostname.lower()


def request_origin_allowed(origin: str, request_host: str, trusted_domains: list[str]) -> bool:
    """Combine ``origin_matches_host`` + ``origin_in_allowlist`` into one predicate."""
    return origin_matches_host(origin, request_host) or origin_in_allowlist(origin, trusted_domains)
