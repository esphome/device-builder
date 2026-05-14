"""Pure helper functions used by the device-state monitor.

No class state, no callbacks — just small string / decode / TXT
record utilities the monitor and its sibling source modules share.
"""

from __future__ import annotations

from functools import lru_cache
from operator import attrgetter
from typing import Any

from zeroconf.asyncio import AsyncServiceInfo

from ...helpers.hostname import is_local_hostname

_ESPHOME_SERVICE_TYPE = "_esphomelib._tcp.local."
# Watched by the shared zeroconf browser so the discovery banner can
# surface a Visit-web-UI link on importable devices running their
# factory firmware's built-in web server. Configured devices already
# get ``web_port`` from the YAML (``web_server:``).
_HTTP_SERVICE_TYPE = "_http._tcp.local."

# Strip ``-`` (Windows) and ``.`` (Cisco) too so a vendored tool or
# future firmware can't slip a non-canonical form into the dedupe
# path or the sidecar.
_MAC_SEPARATORS = str.maketrans("", "", ":-.")


def device_name_from_service(service_name: str) -> str:
    """
    Extract the device name from an mDNS service-instance name.

    Don't substitute hyphens for underscores or vice versa — the
    broadcast preserves the device's ``esphome.name`` verbatim and
    the catalog lookup matches on that exact value.
    """
    return service_name.split(".", maxsplit=1)[0]


def _normalize_mac(value: str) -> str:
    """
    Canonicalise a broadcast MAC to ``XX:XX:XX:XX:XX:XX`` form.

    Returns ``""`` when the input doesn't shape into a 48-bit hex
    MAC — callers treat that the same as "TXT absent" and skip
    the apply path.
    """
    stripped = value.translate(_MAC_SEPARATORS).upper()
    if len(stripped) != 12:
        return ""
    try:
        int(stripped, 16)
    except ValueError:
        return ""
    return ":".join(stripped[i : i + 2] for i in range(0, 12, 2))


def _http_url_from_service_info(device_name: str, info: AsyncServiceInfo) -> str:
    """
    Build ``http://<host>[:port]`` from a populated HTTP service info.

    ``info.server`` is trusted only when it's an ``.local`` hostname;
    anything else (routable hostname, remote SRV target) gets
    rewritten to ``<device_name>.local`` so a malicious or
    misconfigured announcement can't surface a clickable link
    pointing somewhere off-LAN.
    """
    raw_server = info.server.removesuffix(".") if info.server else ""
    host = raw_server if is_local_hostname(raw_server) else f"{device_name}.local"
    port = info.port or 80
    return f"http://{host}{'' if port == 80 else f':{port}'}"


def _decode_mdns_txt_records(txt_dns_records: list[Any]) -> dict[str, str]:
    """
    Decode the freshest cached ``DNSText`` record to a sorted ``key=value`` dict.

    Bare keys (``foo`` with no ``=``) and empty-value entries
    (``foo=``) both collapse to ``None`` in zeroconf's
    ``decoded_properties``; we surface those as ``""`` rather than
    drop them so the ``api_encryption`` empty-string tri-state
    (plaintext-confirmed) survives the round trip.

    Returns ``{}`` when no TXT records are passed or the freshest
    record's ``text`` is not bytes-like. Keys are sorted so the
    wire output is deterministic across snapshots.
    """
    if not txt_dns_records:
        return {}
    latest_txt = max(txt_dns_records, key=attrgetter("created"))
    txt_bytes = latest_txt.text
    if not isinstance(txt_bytes, (bytes, bytearray)):
        return {}
    return dict(_decode_txt_bytes_to_sorted_pairs(bytes(txt_bytes)))


@lru_cache(maxsize=256)
def _decode_txt_bytes_to_sorted_pairs(txt_bytes: bytes) -> tuple[tuple[str, str], ...]:
    """
    Bytes-keyed memoised TXT decode — the reusable hot path.

    The reachability snapshot fires on every observation; keying
    on the immutable raw bytes turns repeat calls against a stable
    fleet into hash-table lookups instead of allocation-heavy
    ``ServiceInfo.text`` re-parses.

    Returns an immutable ``tuple[tuple[str, str], ...]`` rather
    than a dict so a downstream caller mutating the result can't
    poison subsequent cache hits. Callers materialise a fresh
    dict via ``dict(pairs)``.
    """
    # ``service_name`` is required by the ctor but doesn't affect
    # ``set_text`` parsing — pass a placeholder so the cache key
    # stays bytes-only.
    info = AsyncServiceInfo(_ESPHOME_SERVICE_TYPE, f"_decode.{_ESPHOME_SERVICE_TYPE}")
    info.text = txt_bytes
    decoded = info.decoded_properties
    # Filter to string keys *before* sorting — ``decoded`` can
    # contain ``bytes`` keys when a TXT entry fails UTF-8 decode,
    # and ``sorted()`` of a mixed ``str | bytes`` set raises
    # ``TypeError`` in Python 3.
    str_keys = sorted(key for key in decoded if isinstance(key, str))
    pairs: list[tuple[str, str]] = []
    for key in str_keys:
        value = decoded[key]
        pairs.append((key, value if isinstance(value, str) else ""))
    return tuple(pairs)


def _pick_ipv4(addresses: list[str]) -> str:
    """
    Return the first IPv4 address in *addresses*, or the first entry overall.

    ``Device.ip`` only carries one IP, so when a host has both V4
    and V6 we lock onto the V4 entry — friendlier for ICMP across
    subnets and avoids the IPv6 scope-ID gymnastics ``apply_ip``
    consumers aren't prepared for.
    """
    for address in addresses:
        if "." in address and ":" not in address:
            return address
    return addresses[0]
