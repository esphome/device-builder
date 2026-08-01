"""Hostname helpers shared between the DNS cache, ping sweep, and OTA cache args."""

from __future__ import annotations

import re

# ``name_add_mac_suffix: true`` appends ``-<last-3-mac-bytes-hex>`` to
# the configured ``esphome.name`` on the wire. Six lowercase hex digits
# is unambiguous with ESPHome's hostname rules (base names can't end in
# a bare 6-hex MAC suffix unless the user typed it literally).
_MAC_SUFFIX_BROADCAST_RE = re.compile(r"^(.+)-([0-9a-f]{6})$")
_MAC_SEPARATORS = str.maketrans("", "", ":-.")


def default_mdns_address(name: str) -> str:
    """Return the mDNS address ESPHome derives from a device *name* by default."""
    return f"{name}.local"


def mac_suffix_from_address(mac: str) -> str:
    """Return ESPHome's ``name_add_mac_suffix`` tail (last 3 MAC bytes, lowercase hex)."""
    stripped = mac.translate(_MAC_SEPARATORS).lower()
    if len(stripped) != 12:
        return ""
    try:
        int(stripped, 16)
    except ValueError:
        return ""
    return stripped[-6:]


def broadcast_hostname_with_mac_suffix(base_name: str, mac: str) -> str:
    """Return the mDNS hostname ESPHome publishes when ``name_add_mac_suffix`` is on."""
    suffix = mac_suffix_from_address(mac)
    return f"{base_name}-{suffix}" if suffix else base_name


def split_mac_suffix_broadcast(broadcast_name: str) -> tuple[str, str] | None:
    """
    Split a suffixed broadcast hostname into ``(base_name, mac_suffix)``.

    Returns ``None`` when *broadcast_name* doesn't match the
    ``name_add_mac_suffix`` shape.
    """
    match = _MAC_SUFFIX_BROADCAST_RE.match(broadcast_name.lower())
    if match is None:
        return None
    return match.group(1), match.group(2)


def normalize_hostname(hostname: str) -> str:
    """
    Lower-case *hostname* and strip the trailing FQDN dot.

    mDNS / DNS hostnames are case-insensitive and zeroconf often hands
    us names with a trailing ``.`` ; normalising once means cache keys
    and ``.local`` checks compare equal regardless of which form the
    caller passed in.
    """
    return hostname.rstrip(".").lower()


def is_local_hostname(hostname: str) -> bool:
    """Return True when *hostname* is an mDNS ``.local`` name (case/dot insensitive)."""
    return normalize_hostname(hostname).endswith(".local")
