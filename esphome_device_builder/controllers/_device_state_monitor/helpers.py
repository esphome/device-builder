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


# Allowed separators between the six octets of a MAC.
# ESPHome firmware today broadcasts the compact 12-hex-char form
# (no separators); the dashboard's *canonical* form
# (``XX:XX:XX:XX:XX:XX``, applied at ingest by ``_normalize_mac``)
# uses ``:``. We normalize away ``-`` (Windows-style) and ``.``
# (Cisco) too so a future firmware change or vendored tool can't
# slip a non-canonical form into the dedupe path or the sidecar.
_MAC_SEPARATORS = str.maketrans("", "", ":-.")


def _normalize_mac(value: str) -> str:
    """Canonicalise a broadcast MAC to ``XX:XX:XX:XX:XX:XX`` form.

    Strips ``:`` / ``-`` / ``.`` separators, uppercases, validates
    the result is 12 hex chars, then re-inserts ``:`` between every
    octet. Returns ``""`` when the input doesn't shape into a
    48-bit hex MAC — callers treat that the same as "TXT absent"
    and skip the apply path. Done at ingest so the dedupe, sidecar,
    in-memory model, and frontend wire all carry one canonical form
    regardless of which case / separator style the firmware happens
    to broadcast.
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
    """Build ``http://<host>[:port]`` from a populated HTTP service info.

    Single source of truth for the URL shape — ``_apply_http_service_info``
    (browser callback path) and ``_seed_http_url_from_cache`` (late-binding
    path when the HTTP service was already cached before the importable
    arrived) both call this so the format stays consistent.

    ``info.server`` is trusted only when it's an ``.local`` hostname.
    Anything else (a routable hostname, a remote SRV target) gets
    rewritten to ``<device_name>.local`` so a malicious or
    misconfigured announcement can't surface a clickable link
    pointing somewhere off-LAN.
    """
    raw_server = info.server.removesuffix(".") if info.server else ""
    host = raw_server if is_local_hostname(raw_server) else f"{device_name}.local"
    port = info.port or 80
    return f"http://{host}{'' if port == 80 else f':{port}'}"


@lru_cache(maxsize=256)
def _decode_txt_bytes_to_sorted_pairs(txt_bytes: bytes) -> tuple[tuple[str, str], ...]:
    """
    Bytes-keyed memoised TXT decode — the reusable hot path.

    The reachability snapshot fires on every observation; for a
    50-device fleet with the drawer open that's ~50 calls/sec
    against a ``DNSText`` cache where each device's TXT bytes
    rarely change between firmware flashes. The decode itself
    (``ServiceInfo.text`` setter → ``decoded_properties`` →
    sort + filter) costs about an allocation-heavy 50µs per call;
    keying on the immutable raw bytes turns 49 of every 50 of
    those calls into hash-table lookups.

    Returns an immutable ``tuple[tuple[str, str], ...]`` rather
    than a dict so a downstream caller mutating the result can't
    poison subsequent cache hits. Callers materialise a fresh
    dict via ``dict(pairs)``.

    Bytes are content-addressed: two devices broadcasting
    byte-identical TXT payloads share a cache entry, which is
    correct (the decoded output is the same).

    ``maxsize=256`` covers fleet sizes well past the typical
    tens-to-low-hundreds, with headroom for a device that
    re-broadcasts a slightly different TXT payload (firmware
    upgrade, ``config_hash`` change) without immediately
    evicting another device's stable entry. Still bounded so a
    long-running dashboard with rotating device names can't grow
    the cache without limit.
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
    # ``TypeError`` in Python 3. Binding ``value`` to a local also
    # lets mypy narrow with ``isinstance(value, str)`` — narrowing
    # on a subscript expression (``decoded[key]``) doesn't carry
    # across the two references in the inline conditional.
    str_keys = sorted(key for key in decoded if isinstance(key, str))
    pairs: list[tuple[str, str]] = []
    for key in str_keys:
        value = decoded[key]
        pairs.append((key, value if isinstance(value, str) else ""))
    return tuple(pairs)


def _decode_mdns_txt_records(txt_dns_records: list[Any]) -> dict[str, str]:
    """
    Decode the freshest cached ``DNSText`` record into a sorted ``key=value`` dict.

    Reuses ``ServiceInfo.text`` setter so we get zeroconf's canonical
    RFC-6763 split (length-prefixed UTF-8 entries → ``key=value``
    pairs) and ``decoded_properties`` for the UTF-8 decode +
    bad-bytes-to-``None`` handling. Skips ``load_from_cache`` so
    tests can stub the cache with a ``MagicMock``: that helper's
    strict ``DNSCache`` isinstance check would crash the test path,
    and the only thing we need from the cache here is the
    already-fetched TXT bytes.

    Empty / bare-key handling: zeroconf collapses both bare keys
    (``foo`` with no ``=``) and empty-value entries (``foo=`` with
    ``=`` but no value) to the same ``None`` in
    ``decoded_properties``. The diagnostic value is the same in
    both cases — the user wants to see that the key IS present
    even if the value is empty — so we surface those as ``""``
    rather than dropping them. The empty string is the signal the
    upstream ``api_encryption`` tri-state already uses for "device
    confirmed plaintext" (issue #437) and the whole point of the
    debug collapsible is to make those advertisements observable.

    Order stability: zeroconf preserves the bytes-order of the
    raw TXT entries, which can shift on a fresh announce or when
    the cache rebuilds an entry. We sort by key so the wire
    output is deterministic across snapshots, letting downstream
    consumers dedupe with plain equality / ``JSON.stringify``
    instead of comparing dicts set-wise.

    The actual bytes-to-dict decode is delegated to
    ``_decode_txt_bytes_to_sorted_pairs`` so consecutive calls
    with the same TXT bytes reuse the cached result — typical
    for a stable fleet where each device's TXT rarely changes
    between firmware flashes.

    Returns ``{}`` when no TXT records are passed or the freshest
    record's ``text`` attribute is missing / not bytes-like.
    """
    if not txt_dns_records:
        return {}
    latest_txt = max(txt_dns_records, key=attrgetter("created"))
    txt_bytes = latest_txt.text
    if not isinstance(txt_bytes, (bytes, bytearray)):
        return {}
    return dict(_decode_txt_bytes_to_sorted_pairs(bytes(txt_bytes)))


def device_name_from_service(service_name: str) -> str:
    """Extract the device name from an mDNS service-instance name.

    The mDNS service announcement is
    ``<device-name>._esphomelib._tcp.local.``; the left-hand label is
    the device's ``esphome.name`` *verbatim* — modern configs use
    ``friendly_name_slugify``-style names with hyphens
    (``apollo-r-pro-1-eth-5938e0``) and the broadcast preserves them.
    Older underscored names (``my_device``) are likewise broadcast as
    given. Don't substitute hyphens for underscores or vice versa or
    the catalog lookup will silently miss every match.
    """
    return service_name.split(".", maxsplit=1)[0]


def _pick_ipv4(addresses: list[str]) -> str:
    """
    Return the first IPv4 address in *addresses*, or the first entry overall.

    ``Device.ip`` only carries one IP, so when a host has both V4 and V6
    we lock onto the V4 entry — it's friendlier for ICMP across subnets
    and avoids the IPv6 scope-ID gymnastics that ``apply_ip`` consumers
    aren't prepared for. Callers that need every address (CLI cache args)
    should iterate the list themselves rather than going through this.
    """
    for address in addresses:
        if "." in address and ":" not in address:
            return address
    return addresses[0]
