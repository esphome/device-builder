"""Entry point: python -m esphome_device_builder [options]."""

from __future__ import annotations

import argparse
import logging
import os
from logging.handlers import RotatingFileHandler

from .constants import DEFAULT_HOST, DEFAULT_INGRESS_PORT, DEFAULT_PORT
from .controllers.config import DashboardSettings
from .device_builder import DeviceBuilder

_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_MAX_LOG_SIZE = 5_000_000  # 5 MB
_LOGGER_NAME = "esphome_device_builder"

_LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def _setup_logging(log_level: str, log_file: str | None = None) -> None:
    """Set up logging with console + optional file handler."""
    level = _LOG_LEVELS.get(log_level.lower(), logging.INFO)

    logging.basicConfig(level=level, format=_FORMAT, datefmt=_DATE_FORMAT)

    if log_file:
        file_handler = RotatingFileHandler(log_file, maxBytes=_MAX_LOG_SIZE, backupCount=1)
        file_handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT))
        logging.getLogger().addHandler(file_handler)

    logging.getLogger(_LOGGER_NAME).setLevel(level)

    # Silence noisy libraries
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("zeroconf").setLevel(logging.WARNING)


def main() -> None:
    """Run the ESPHome Device Builder."""
    parser = argparse.ArgumentParser(
        description="ESPHome Device Builder",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "configuration",
        nargs="?",
        default="./configs",
        help="Path to the ESPHome configuration directory",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="HTTP port to listen on")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Host/IP to bind to")
    parser.add_argument(
        "--username",
        default="",
        help=(
            "Dashboard username (must be paired with --password; falls back to $ESPHOME_USERNAME)"
        ),
    )
    parser.add_argument(
        "--password",
        default="",
        help=(
            "Dashboard password (must be paired with --username; falls back to $ESPHOME_PASSWORD)"
        ),
    )
    parser.add_argument("--ha-addon", action="store_true", help="Running as HA add-on")
    parser.add_argument(
        "--ingress-port",
        type=int,
        default=DEFAULT_INGRESS_PORT,
        help="Port for the trusted HA Ingress site (only used with --ha-addon)",
    )
    parser.add_argument(
        "--ingress-host",
        default="",
        help=(
            "Bind address for the HA Ingress site (defaults to all interfaces "
            "inside the addon container)"
        ),
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["debug", "info", "warning", "error"],
        help="Log level",
    )
    parser.add_argument("--log-file", default=None, help="Log to file (rotated)")
    parser.add_argument(
        "--dev",
        action="store_true",
        help=(
            "Development mode: serve ``index.html`` with ``Cache-Control: "
            "no-cache`` so the browser always picks up a freshly-rebuilt "
            "frontend wheel. Disabled by default — the browser's heuristic "
            "is fine in production."
        ),
    )
    parser.add_argument(
        "--trusted-domains",
        default=None,
        help=(
            "Comma-separated hostnames the WebSocket handshake trusts "
            "(case-insensitive, port-tolerant). Two effects when password "
            "auth is on AND the request carries an Origin header: (1) "
            "accept cross-origin connections whose Origin header's "
            "hostname is in the list — required for reverse-proxy "
            "deployments where Origin is ``dashboard.example.com`` but the "
            "upstream Host is ``localhost``; (2) reject any connection "
            "whose Host header isn't in the list — defense in depth against "
            "DNS rebinding. Both gates skip Origin-less requests (CLI "
            "tools, HA integration, direct websockets clients) since "
            "DNS-rebinding is a browser-only attack vector and those "
            "clients are already gated by bearer-token auth. Default "
            "(flag unset) consults the $ESPHOME_TRUSTED_DOMAINS env var "
            '(legacy ESPHome dashboard compatibility); pass --trusted-domains "" '
            "to explicitly ignore the env var and disable both checks. "
            "Use ``*`` as the only entry to opt out of host-restriction "
            "while keeping cross-origin acceptance permissive."
        ),
    )

    args = parser.parse_args()

    _validate_credentials(parser, args)

    _setup_logging(args.log_level, args.log_file)

    settings = DashboardSettings()
    settings.parse_args(args)

    _warn_if_unprotected(settings)

    device_builder = DeviceBuilder(settings)
    device_builder.run()


def _validate_credentials(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject mismatched --username / --password (or env equivalents)."""
    has_user = bool(args.username or os.getenv("ESPHOME_USERNAME"))
    has_pass = bool(args.password or os.getenv("ESPHOME_PASSWORD"))
    if has_user != has_pass:
        parser.error(
            "--username and --password must both be set (or both unset). "
            "Use $ESPHOME_USERNAME / $ESPHOME_PASSWORD env vars as alternatives."
        )


def _warn_if_unprotected(settings: DashboardSettings) -> None:
    """Print a banner when starting without any authentication boundary."""
    if settings.using_password:
        return
    # HA add-on installs are exempt — the supervisor's ingress proxy
    # authenticates upstream of the trusted site.
    if settings.create_ingress_site:
        return
    banner = "=" * 70
    logging.getLogger(_LOGGER_NAME).warning(
        "\n%s\n"
        " WARNING: Dashboard is running WITHOUT AUTHENTICATION.\n"
        " Anyone with network access to %s:%d can manage your devices.\n"
        " Set --username and --password (or $ESPHOME_USERNAME / $ESPHOME_PASSWORD) to enable.\n"
        "%s",
        banner,
        settings.host,
        settings.port,
        banner,
    )


if __name__ == "__main__":
    main()
