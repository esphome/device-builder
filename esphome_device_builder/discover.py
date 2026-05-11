"""Browse the LAN for ESPHome dashboards and print them.

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


def _decode(data: str | bytes | None) -> str:
    """Decode a TXT-record value to ``str``, or return ``unknown``."""
    if data is None:
        return _UNKNOWN
    if isinstance(data, bytes):
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("utf-8", errors="replace")
    return data


def _truncate_pin(pin: str) -> str:
    """Trim a 64-hex pin to its first 12 chars so it fits in the column.

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
    """Print one row per browse event.

    Resolves the cached :class:`AsyncServiceInfo` synchronously
    (the browser already populated zeroconf's cache before
    dispatching the callback) so the print order matches the
    wire order. ``Removed`` events still have the cached fields
    available, so an OFFLINE row carries the same metadata the
    last ONLINE row did — useful for spotting which exact
    dashboard just dropped off the network.
    """
    short_name = name.partition(".")[0]
    state = "OFFLINE" if state_change is ServiceStateChange.Removed else "ONLINE"
    info = AsyncServiceInfo(service_type, name)
    info.load_from_cache(zeroconf)

    properties = info.properties
    server_version = _decode(properties.get(b"server_version"))
    esphome_version = _decode(properties.get(b"esphome_version"))
    pin_sha256 = _decode(properties.get(b"pin_sha256"))
    remote_build_port = _decode(properties.get(b"remote_build_port"))

    address = ""
    if addresses := info.ip_addresses_by_version(IPVersion.V4Only):
        address = str(addresses[0])
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


async def _run(argv: list[str]) -> None:
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
    args = parser.parse_args(argv[1:])

    logging.basicConfig(
        format="%(asctime)s.%(msecs)03d %(levelname)-8s %(message)s",
        level=logging.DEBUG if args.verbose else logging.INFO,
        datefmt="%Y-%m-%d %H:%M:%S",
    )
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


def main() -> None:
    """CLI entry point."""
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run(sys.argv))


if __name__ == "__main__":
    main()
    sys.exit(0)
