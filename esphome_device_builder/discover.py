"""
Browse the LAN for ESPHome dashboards and print them.

CLI helper that browses ``_esphomebuilder._tcp.local.`` and prints
every dashboard reachable on the LAN. Same shape as
``aioesphomeapi-discover``; one-shot browse, no pairing flow, no
persistence. Useful for confirming an offloader can see a build
server, sanity-checking which TXT fields are populated, and
verifying the rest of the network's pin fingerprints out-of-band
before clicking ``Pair``.

Run as ``esphome-device-builder-discover`` (the package's
``[project.scripts]`` entry point) or ``python -m
esphome_device_builder.discover``. The browse runs until Ctrl-C;
rows print as services appear / disappear so a watcher can see
the network's churn live.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import re
import sys

from zeroconf import IPVersion, ServiceStateChange, Zeroconf
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from .helpers.dashboard_advertise import SERVICE_TYPE

_FORMAT = "{: <7}|{: <24}|{: <21}|{: <18}|{: <16}|{: <12}|{: <16}"
_COLUMN_NAMES = (
    "Status",
    "Name",
    "Address:Port",
    "Server",
    "ESPHome",
    "RB Port",
    "Pin (sha256)",
)
_UNKNOWN = "unknown"

# Per-column display caps for peer-supplied mDNS labels, derived from the
# _FORMAT widths so a hostile broadcaster can't widen a column by stuffing a
# long value; deriving from _FORMAT keeps the caps in lock-step if the table
# layout is ever re-tuned.
_COLUMN_WIDTHS = tuple(int(w) for w in re.findall(r"<\s*(\d+)", _FORMAT))
if len(_COLUMN_WIDTHS) != len(_COLUMN_NAMES):
    # Runtime check, not `assert`, so the invariant still holds under
    # `python -O` (which strips assert statements).
    raise RuntimeError(
        "_FORMAT width count must match _COLUMN_NAMES; update one and the other together"
    )
_MAX_NAME_DISPLAY = _COLUMN_WIDTHS[_COLUMN_NAMES.index("Name")]
_MAX_SERVER_DISPLAY = _COLUMN_WIDTHS[_COLUMN_NAMES.index("Server")]
_MAX_ESPHOME_DISPLAY = _COLUMN_WIDTHS[_COLUMN_NAMES.index("ESPHome")]
_MAX_PORT_DISPLAY = _COLUMN_WIDTHS[_COLUMN_NAMES.index("RB Port")]
# Pin column is 16 chars wide but `_truncate_pin` collapses a full 64-hex pin
# to 12 chars + ellipsis at print time, so the raw cap stays at 64 to keep
# legitimate pins intact; an oversized hostile value is still bounded by the
# subsequent truncation.
_MAX_PIN_DISPLAY = 64


def main() -> None:
    """CLI entry point.

    All filesystem-touching bootstrap (argparse construction,
    ``logging.basicConfig``) runs synchronously here, before the
    asyncio loop starts. :func:`_run` is then a pure async
    orchestration coroutine; keeps Python 3.14's blockbuster
    suite quiet (its argparse constructor calls ``os.stat`` via
    gettext, which would otherwise trip the event-loop guard).
    """
    args = _build_parser().parse_args(sys.argv[1:])
    logging.basicConfig(
        format="%(asctime)s.%(msecs)03d %(levelname)-8s %(message)s",
        level=logging.DEBUG if args.verbose else logging.INFO,
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run(args))


def _build_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser.

    Factored out of :func:`main` so the construction site is
    callable from tests without the rest of the bootstrap, and
    so :func:`_run` can stay free of filesystem-touching work.
    Python 3.14's :class:`argparse.ArgumentParser` constructor
    calls :func:`gettext.gettext`, which does an ``os.stat``
    under the hood; running that inside an asyncio test
    triggers blockbuster's "no blocking calls on the event
    loop" guard. Keeping argparse out of :func:`_run` means the
    CLI's async body is pure orchestration and the test stays
    quiet on every Python version.
    """
    parser = argparse.ArgumentParser(
        "esphome-device-builder-discover",
        description=(
            "Browse the LAN for ESPHome dashboards advertising "
            f"``{SERVICE_TYPE}`` and print each one's address, "
            "versions, remote-build port, and cert pin (truncated)."
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging (including zeroconf's own).",
    )
    return parser


async def _run(args: argparse.Namespace) -> None:
    """
    Orchestrate the browse against pre-parsed *args*.

    Async body deliberately doesn't do any filesystem-touching
    work (no argparse construction, no logging.basicConfig with
    a file handler) so blockbuster's event-loop guard stays
    quiet under test. The caller in :func:`main` resolves all
    of that synchronously before entering the loop.
    """
    if args.verbose:
        logging.getLogger("zeroconf").setLevel(logging.DEBUG)

    aiozc = AsyncZeroconf()
    browser = AsyncServiceBrowser(
        aiozc.zeroconf,
        SERVICE_TYPE,
        handlers=[_on_service_state_change],
    )
    print(_FORMAT.format(*_COLUMN_NAMES))
    print("-" * 120)

    try:
        await asyncio.Event().wait()
    finally:
        await browser.async_cancel()
        await aiozc.async_close()


def _safe_label(raw: str, limit: int) -> str:
    """Strip non-printables and length-cap a peer-supplied label for stdout."""
    return "".join(filter(str.isprintable, raw))[:limit]


def _decode_mdns_label_or_unknown(data: str | bytes | None, limit: int = _MAX_NAME_DISPLAY) -> str:
    """Decode peer-supplied mDNS bytes, strip non-printables, length-cap."""
    if data is None:
        return _UNKNOWN
    if isinstance(data, bytes):
        # A device on the LAN can broadcast arbitrary bytes; use "replace" so
        # a malformed UTF-8 payload doesn't raise out of the zeroconf callback.
        data = data.decode("utf-8", "replace")
    return _safe_label(data, limit)


def _truncate_pin(pin: str) -> str:
    """
    Trim a 64-hex pin to its first 12 chars so it fits in the column.

    The full pin is what the pairing flow asserts; the truncated
    head is enough for an at-a-glance "is that the same fingerprint
    I saw on the other dashboard's identity card?" sanity check.
    """
    if pin == _UNKNOWN or len(pin) <= 12:
        return pin
    return f"{pin[:12]}…"


def _on_service_state_change(
    zeroconf: Zeroconf,
    service_type: str,
    name: str,
    state_change: ServiceStateChange,
) -> None:
    """
    Print one row per browse event.

    Resolves the cached :class:`AsyncServiceInfo` synchronously
    (the browser already populated zeroconf's cache before
    dispatching the callback) so the print order matches the
    wire order. ``Removed`` events still have the cached fields
    available, so an OFFLINE row carries the same metadata the
    last ONLINE row did, useful for spotting which exact
    dashboard just dropped off the network.

    Address resolution prefers IPv4 (operators read the
    Address:Port column at a glance and IPv4 fits more cleanly
    in the fixed-width column) but falls back to IPv6 when no
    IPv4 is advertised. ``parsed_scoped_addresses`` matches the
    rest of the project's peer-discovery sites (cf.
    :mod:`controllers._device_state_monitor` /
    :mod:`controllers.remote_build.controller`).
    """
    # The mDNS service name is peer-controlled; sanitize before printing so a
    # hostile broadcaster can't inject ANSI escapes / newlines / null bytes
    # into the terminal via the instance label.
    short_name = _safe_label(name.partition(".")[0], _MAX_NAME_DISPLAY)
    state = "OFFLINE" if state_change is ServiceStateChange.Removed else "ONLINE"
    info = AsyncServiceInfo(service_type, name)
    # ``load_from_cache`` returns ``False`` when the browser
    # callback fired before the resolve completed; in that case
    # ``info.properties`` can be ``None``. Coalesce to an empty
    # dict so the TXT-field gets fall through to the ``unknown``
    # sentinel rather than crashing the callback (which would
    # silently kill the browse loop). A subsequent state-change
    # event for the same name will land a complete row once the
    # resolve catches up.
    info.load_from_cache(zeroconf)
    properties = info.properties or {}
    server_version = _decode_mdns_label_or_unknown(
        properties.get(b"server_version"), _MAX_SERVER_DISPLAY
    )
    esphome_version = _decode_mdns_label_or_unknown(
        properties.get(b"esphome_version"), _MAX_ESPHOME_DISPLAY
    )
    pin_sha256 = _decode_mdns_label_or_unknown(properties.get(b"pin_sha256"), _MAX_PIN_DISPLAY)
    remote_build_port = _decode_mdns_label_or_unknown(
        properties.get(b"remote_build_port"), _MAX_PORT_DISPLAY
    )

    address = ""
    if v4_addresses := info.ip_addresses_by_version(IPVersion.V4Only):
        address = str(v4_addresses[0])
    elif scoped := info.parsed_scoped_addresses(IPVersion.All):
        # IPv6-only dashboard, or a dashboard whose IPv4 hasn't
        # resolved yet. Take the first scoped address so the
        # column gets a meaningful value rather than ``unknown``;
        # the scope_id suffix (``...%eth0``) helps the operator
        # tell link-local entries apart from globally-routable
        # ones.
        address = scoped[0]
    endpoint = f"{address}:{info.port}" if address and info.port else address or _UNKNOWN

    print(
        _FORMAT.format(
            state,
            short_name,
            endpoint,
            server_version,
            esphome_version,
            remote_build_port,
            _truncate_pin(pin_sha256),
        )
    )


if __name__ == "__main__":  # pragma: no cover
    main()
    sys.exit(0)
