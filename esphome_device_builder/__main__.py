"""Entry point: python -m esphome_device_builder [options]."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from contextlib import suppress
from logging.handlers import RotatingFileHandler
from types import TracebackType
from typing import TYPE_CHECKING, cast

from colorlog import ColoredFormatter

from .constants import (
    DEFAULT_HOST,
    DEFAULT_INGRESS_PORT,
    DEFAULT_PORT,
    DEFAULT_REMOTE_BUILD_PORT,
    __version__,
)
from .helpers.logging import activate_log_queue_handler

if TYPE_CHECKING:
    from .controllers.config import DashboardSettings

_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)s (%(threadName)s) [%(name)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_MAX_LOG_SIZE = 5_000_000  # 5 MB
_LOGGER_NAME = "esphome_device_builder"

_LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

_LOG_COLORS = {
    "DEBUG": "cyan",
    "INFO": "green",
    "WARNING": "yellow",
    "ERROR": "red",
    "CRITICAL": "red",
}


def _setup_logging(log_level: str, log_file: str | None = None) -> None:
    """Set up logging with a coloured console handler and an optional rotating file."""
    level = _LOG_LEVELS.get(log_level.lower(), logging.INFO)

    logging.getLogger().setLevel(level)

    # Install our own ``StreamHandler`` rather than going through
    # ``basicConfig`` — the latter is a no-op when handlers are
    # already configured (e.g., under some test runners), which would
    # leave the colour formatter unattached.
    colorfmt = f"%(log_color)s{_FORMAT}%(reset)s"
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(
        ColoredFormatter(
            colorfmt,
            datefmt=_DATE_FORMAT,
            reset=True,
            log_colors=_LOG_COLORS,
        )
    )
    logging.getLogger().addHandler(console_handler)

    # Route ``warnings.warn`` through the logging system instead of
    # raw stderr so the queue handler and our formatter apply.
    logging.captureWarnings(capture=True)

    if log_file:
        file_handler = RotatingFileHandler(log_file, maxBytes=_MAX_LOG_SIZE, backupCount=1)
        # Fresh log file per process start.
        with suppress(OSError):
            file_handler.doRollover()
        file_handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT))
        logging.getLogger().addHandler(file_handler)

    logging.getLogger(_LOGGER_NAME).setLevel(level)

    # Silence noisy libraries
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("zeroconf").setLevel(logging.WARNING)

    # Route uncaught main-thread and worker-thread exceptions through
    # the logging system so they hit the same console + rotating-file
    # destinations as everything else, instead of going to bare stderr.
    sys.excepthook = _log_uncaught_exception
    threading.excepthook = _log_uncaught_thread_exception

    # Has to be the last step — handlers added after this run inline
    # on the calling thread instead of being offloaded to the listener.
    activate_log_queue_handler()


def _exit_on_startup_sigterm(_signum: int, _frame: object) -> None:
    """Exit cleanly (0) on a SIGTERM received before the run loop is up."""
    raise SystemExit(0)


def main() -> None:
    """Run the ESPHome Device Builder."""
    # Trap SIGTERM for the whole startup phase. aiohttp installs the
    # run-loop's handler only once the server is up; a SIGTERM landing in
    # the seconds before that (slow esphome import, catalog load, mDNS
    # bring-up) would otherwise hit the OS default disposition and exit
    # 143, which a supervisor reports as "did not handle SIGTERM".
    # ``web.run_app`` replaces this with its own graceful handler once
    # serving begins.
    signal.signal(signal.SIGTERM, _exit_on_startup_sigterm)

    parser = argparse.ArgumentParser(
        description="ESPHome Device Builder",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=_format_version(),
        help="Print version information and exit",
    )
    parser.add_argument(
        "configuration",
        nargs="?",
        default="./configs",
        help="Path to the ESPHome configuration directory",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="HTTP port to listen on")
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=(
            "Host/IP to bind to. Accepts an IP literal, a hostname, or a "
            "local network interface name (e.g. 'eth0'); an interface name "
            "binds to every IPv4 / IPv6 address currently assigned to "
            "that interface. Useful in Docker host-network mode where "
            "the container's LAN IP isn't known in advance"
        ),
    )
    parser.add_argument(
        "--username",
        default="",
        help=(
            "Deprecated; use $ESPHOME_USERNAME instead. Dashboard username "
            "(must be paired with --password). The flag will be removed in "
            "a future release"
        ),
    )
    parser.add_argument(
        "--password",
        default="",
        help=(
            "Deprecated; use $ESPHOME_PASSWORD instead. Dashboard password "
            "(must be paired with --username). The flag will be removed in "
            "a future release; values passed on the command line are visible "
            "to every other local user via process listings"
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
            "inside the addon container). Accepts an IP literal or a local "
            "network interface name (e.g. 'eth0')"
        ),
    )
    parser.add_argument(
        "--remote-build-port",
        type=int,
        # ``SUPPRESS`` keeps ``ArgumentDefaultsHelpFormatter`` from
        # rendering a contradictory ``(default: None)`` next to the
        # help text; the real default lives in
        # ``DashboardSettings.parse_args`` (env-var fallback +
        # ``DEFAULT_REMOTE_BUILD_PORT``).
        default=argparse.SUPPRESS,
        help=(
            f"Plain-TCP port for the remote-build peer-link Noise WS "
            f"(default {DEFAULT_REMOTE_BUILD_PORT} or "
            f"$ESPHOME_REMOTE_BUILD_PORT; only bound when remote-build "
            "is enabled in Settings)"
        ),
    )
    parser.add_argument(
        "--remote-build-host",
        # Real default (``0.0.0.0``) lives in
        # ``DashboardSettings.parse_args`` so the env-var fallback
        # and the CLI flag share one resolution path. Suppressing
        # argparse's default rendering keeps the help text from
        # contradicting that resolution order.
        default=argparse.SUPPRESS,
        help=(
            "Bind address for the remote-build peer-link receiver. "
            "Defaults to 0.0.0.0 (all interfaces) so paired peers on "
            "the LAN can reach the receiver; the peer-link's "
            "security is Noise + pre-shared pin, independent of bind "
            "address. Override (e.g. 127.0.0.1) only if you want to "
            "restrict the receiver to a specific interface. Accepts an "
            "IP literal or a local network interface name (e.g. "
            "'eth0'); an interface name binds to every IPv4 / IPv6 "
            "address currently assigned to that interface. Falls back "
            "to $ESPHOME_REMOTE_BUILD_HOST when unset. Only bound when "
            "remote-build is enabled in Settings"
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

    _warn_deprecated_credential_flags(args)

    # ``--version`` / ``--help`` exit above before reaching this
    # point, so the lazy imports below are reachable only when the
    # user actually meant to run the dashboard. Gate on
    # ``_esphome_version`` (the same probe ``_format_version`` uses)
    # so the missing-extra detection has a single source of truth;
    # surface an actionable hint in place of the raw
    # ``ModuleNotFoundError`` traceback (#919).
    if _esphome_version() is None:
        logging.getLogger(_LOGGER_NAME).error(
            "Running esphome-device-builder needs the 'esphome' "
            "package; reinstall with the [esphome] extra: "
            "pip install 'esphome-device-builder[esphome]'"
        )
        sys.exit(1)

    from esphome.core import CORE  # noqa: PLC0415

    from .controllers.config import DashboardSettings  # noqa: PLC0415
    from .device_builder import DeviceBuilder  # noqa: PLC0415
    from .helpers.single_instance import ensure_single_execution  # noqa: PLC0415
    from .helpers.windows_build_paths import windows_short_build_paths  # noqa: PLC0415

    settings = DashboardSettings()
    settings.parse_args(args)

    _warn_if_unprotected(settings)

    # Keyed on ``CORE.data_dir`` (not ``config_dir``) so the HA
    # addon's Prod/Beta/DEV flavors — each with its own per-instance
    # ``/data`` but a shared ``/config/esphome`` mount — can run in
    # parallel. ``CORE.data_dir`` honours ``is_ha_addon()`` /
    # ``$ESPHOME_DATA_DIR`` and falls back to ``<config_dir>/.esphome``
    # — ``parse_args`` above sets ``CORE.config_path``, which the
    # fallback needs.
    # Windows: relocate the build tree to a short, space-free root (MAX_PATH + spaces)
    # first, so the lock lands in the relocated dir (CORE.data_dir is evaluated after the
    # relocation enters) and migration runs before any lock file is held open in old .esphome.
    with (
        windows_short_build_paths(settings.config_dir),
        ensure_single_execution(CORE.data_dir) as lock,
    ):
        if lock.exit_code is not None:
            sys.exit(lock.exit_code)
        device_builder = DeviceBuilder(settings)
        device_builder.run()


def _log_uncaught_exception(
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_traceback: TracebackType | None,
) -> None:
    """Forward an uncaught main-thread exception into ``logger.exception``."""
    logging.getLogger().exception(
        "Uncaught exception",
        exc_info=(exc_type, exc_value, exc_traceback),
    )


def _log_uncaught_thread_exception(args: threading.ExceptHookArgs) -> None:
    """Forward an uncaught worker-thread exception into ``logger.exception``."""
    # ``threading.ExceptHookArgs.exc_value`` is typed
    # ``BaseException | None`` (the docs note threads can be killed
    # without an exception object), but ``logger.exception``'s
    # exc_info-triple form rejects ``None`` for the value slot. The
    # runtime accepts the malformed-but-documented shape and renders
    # "no exception" cleanly; ``# type: ignore[arg-type]`` keeps the
    # typeshed strictness while preserving the runtime behaviour.
    logging.getLogger().exception(
        "Uncaught thread exception",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),  # type: ignore[arg-type]
    )


def _esphome_version() -> str | None:
    """
    Return the bundled ESPHome version, or ``None`` if the extra is missing.

    Narrows to ``ModuleNotFoundError`` rooted at ``esphome``; an
    ``ImportError`` raised from *inside* ``esphome.const`` (a
    broken install, not a missing one) propagates so callers like
    the ``main()`` gate don't misclassify it as "not installed".
    """
    try:
        from esphome.const import __version__ as version  # noqa: PLC0415
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.split(".", 1)[0] == "esphome":
            return None
        raise
    # ``esphome`` ships no type stubs, so ``__version__`` arrives as
    # ``Any`` and the raw return trips ``no-any-return``. Cast at the
    # boundary — runtime contract is the documented version string.
    return cast("str | None", version)


def _format_version() -> str:
    """
    Build the string shown by ``--version``.

    Always reports the device builder package version (read from the
    installed wheel's metadata, which the release workflow stamps via
    ``pyproject.toml``). Appends the bundled ESPHome version in
    parentheses when the optional ``[esphome]`` extra is importable —
    that's the matching pair an operator pastes into a bug report.
    """
    base = f"esphome-device-builder {__version__}"
    esphome = _esphome_version()
    if esphome is None:
        return base
    return f"{base} (esphome {esphome})"


def _validate_credentials(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject mismatched --username / --password (or env equivalents)."""
    has_user = bool(args.username or os.getenv("ESPHOME_USERNAME"))
    has_pass = bool(args.password or os.getenv("ESPHOME_PASSWORD"))
    if has_user != has_pass:
        parser.error(
            "--username and --password must both be set (or both unset). "
            "Use $ESPHOME_USERNAME / $ESPHOME_PASSWORD env vars as alternatives."
        )


def _warn_deprecated_credential_flags(args: argparse.Namespace) -> None:
    """Log a deprecation warning when --username / --password are used."""
    if not (args.username or args.password):
        return
    logging.getLogger(_LOGGER_NAME).warning(
        "DEPRECATION: --username / --password are deprecated and will be "
        "removed in a future release. Use $ESPHOME_USERNAME / "
        "$ESPHOME_PASSWORD env vars instead; command-line arguments are "
        "visible to every other local user via process listings."
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
        " Set $ESPHOME_USERNAME / $ESPHOME_PASSWORD env vars to enable.\n"
        "%s",
        banner,
        settings.host,
        settings.port,
        banner,
    )


if __name__ == "__main__":
    main()
