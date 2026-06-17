"""Dashboard settings parsed from CLI args and environment."""

from __future__ import annotations

import hmac
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from esphome.core import CORE
from esphome.helpers import get_bool_env
from esphome.helpers import write_file as atomic_write_file

from ...constants import DEFAULT_INGRESS_PORT, DEFAULT_REMOTE_BUILD_PORT, SECRETS_FILENAME
from ...helpers.api import CommandError
from ...helpers.auth import hash_password
from ...helpers.secrets_state import PLACEHOLDER_WIFI_PASSWORD, PLACEHOLDER_WIFI_SSID
from ...models import ErrorCode

_LOGGER = logging.getLogger(__name__)

_DASHBOARD_SENTINEL_FILE = "___DASHBOARD_SENTINEL___.yaml"


@dataclass
class DashboardSettings:
    """Application settings parsed from CLI args and environment."""

    config_dir: Path = field(default_factory=Path)
    absolute_config_dir: Path | None = None
    username: str = ""
    password_hash: bytes = field(default_factory=bytes)
    using_password: bool = False
    on_ha_addon: bool = False
    log_level: str = "info"
    port: int = 6052
    host: str = "0.0.0.0"
    ingress_port: int = DEFAULT_INGRESS_PORT
    ingress_host: str = ""
    # Plain-TCP port for the remote-build peer-link receiver site
    # (issue #106). The transport is Noise XX over plain HTTP/WS,
    # not TLS — Noise provides mutual auth + forward secrecy +
    # confidentiality at the application layer. The
    # site is only bound when ``RemoteBuildSettings.enabled`` is set;
    # default-off keeps the listener inactive on installs that
    # haven't opted in. Lives separately from ``port`` because the
    # peer-link's auth gate (Noise + pre-shared pin pairing) is
    # independent from the dashboard's WS gate (loopback / login).
    remote_build_port: int = DEFAULT_REMOTE_BUILD_PORT
    # Bind address for the remote-build peer-link receiver. Defaults
    # to all interfaces — the feature's whole point is letting paired
    # peers on the LAN reach this dashboard, and the security gate is
    # Noise + pre-shared pin (not bind address). Lives separately from
    # ``host`` because the HTTP/WS dashboard often binds to
    # ``127.0.0.1`` (desktop app loopback security model) while the
    # peer-link still needs to be LAN-reachable. Operators who want
    # to lock the receiver to a specific NIC can override via
    # ``--remote-build-host`` / ``$ESPHOME_REMOTE_BUILD_HOST``. Accepts
    # an IP literal or a local interface name (e.g. ``eth0``); the
    # latter is resolved at bind time to every IPv4 / IPv6 address
    # on the interface (see :func:`helpers.network_interfaces.resolve_bind_host`).
    remote_build_host: str = "0.0.0.0"
    # In dev mode the SPA shell is served with ``Cache-Control: no-cache``
    # so a re-deployed wheel isn't masked by a browser-cached
    # ``index.html`` pointing at a now-deleted hashed bundle. In
    # production we let the browser apply its default heuristic; the
    # hashed bundles are still served as ``immutable`` regardless.
    dev_mode: bool = False
    # Hostnames we trust for cross-origin / Host validation in the
    # WebSocket handshake. Carries the legacy
    # ``ESPHOME_TRUSTED_DOMAINS`` semantics from the upstream
    # dashboard, plus a DNS-rebinding-defense Host check:
    #
    #   * Origin allowlist - when the browser's Origin header
    #     doesn't match the request's Host (reverse-proxy hostname
    #     mismatch), accept the connection if Origin's hostname is
    #     in this list. Fixes the
    #     "lose-dashboard-access-behind-nginx" papercut.
    #   * Host allowlist - reject the request entirely if its Host
    #     header isn't in this list. Defense in depth against DNS
    #     rebinding, on top of the existing per-IP-rate-limited
    #     ``auth/login`` gate.
    #
    # Empty list = both checks disabled (existing strict
    # Origin/Host equality is the only gate; no Host allowlist).
    # ``"*"`` is the explicit "match anything" escape hatch for
    # operators who want to acknowledge the knob without
    # restricting hosts.
    trusted_domains: list[str] = field(default_factory=list)

    def parse_args(self, args: Any) -> None:
        """Parse CLI arguments into settings."""
        self.on_ha_addon = getattr(args, "ha_addon", False)
        # Env-var fallback uses ``ESPHOME_*`` rather than the legacy
        # dashboard's bare ``USERNAME`` / ``PASSWORD``: the bare names
        # collide with login-shell / Windows system vars (``$USERNAME``
        # is the OS user on both), which would silently promote the
        # OS user to the dashboard username when only ``--password``
        # / ``$ESPHOME_PASSWORD`` is set. Intentional divergence from
        # ``esphome/dashboard/settings.py``.
        username = getattr(args, "username", None) or os.getenv("ESPHOME_USERNAME") or ""
        password = getattr(args, "password", None) or os.getenv("ESPHOME_PASSWORD") or ""
        self.username = username
        self.using_password = bool(username and password)
        if self.using_password:
            self.password_hash = hash_password(password)
        self.config_dir = Path(args.configuration)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.absolute_config_dir = self.config_dir.resolve()
        # Ensure secrets.yaml exists (ESPHome fails if !secret references
        # can't find it). Atomic write — a crash mid-write would leave the
        # user with a half-bootstrap'd secrets file and the next startup
        # would see ``not exists() == False`` on the partial and skip
        # this branch, leaving them stuck. ``write_file`` stages in a
        # sibling tempfile + ``shutil.move`` so the file is either fully
        # there or not at all.
        #
        # Use non-empty placeholder strings rather than ``""``: ESPHome's
        # ``wifi`` validator rejects an empty SSID with
        # "SSID can't be empty.", so a fresh-install ``create_device``
        # whose generated YAML uses ``!secret wifi_ssid`` would
        # validation-fail before the device is even saved
        # ("Failed to create device: SSID can't be empty."). The
        # placeholders validate clean and clearly signal to the user
        # that the values need to be replaced before flashing —
        # ``OnboardingController`` reads the same constants from
        # ``helpers.secrets_state`` to detect the unconfigured state
        # and surface the setup wizard.
        secrets_path = self.config_dir / SECRETS_FILENAME
        if not secrets_path.exists():
            atomic_write_file(
                secrets_path,
                "# Secrets — referenced from device configs via !secret\n"
                "# Replace these placeholders with your real Wi-Fi\n"
                "# credentials before flashing or installing OTA.\n"
                f'wifi_ssid: "{PLACEHOLDER_WIFI_SSID}"\n'
                f'wifi_password: "{PLACEHOLDER_WIFI_PASSWORD}"\n',
            )
        self.log_level = getattr(args, "log_level", "info")
        self.port = getattr(args, "port", 6052)
        self.host = getattr(args, "host", "0.0.0.0")
        self.ingress_port = getattr(args, "ingress_port", DEFAULT_INGRESS_PORT)
        self.ingress_host = getattr(args, "ingress_host", "") or ""
        # ``--remote-build-port`` (or ``$ESPHOME_REMOTE_BUILD_PORT``).
        # Precedence mirrors ``--trusted-domains`` below: an explicit
        # CLI value (including the default) wins; ``None`` means
        # "flag not set, consult the env var". Container deployments
        # that fix the CMD in the Dockerfile and override via env
        # can flip the listener port without rebuilding the image.
        cli_remote_build_port = getattr(args, "remote_build_port", None)
        if cli_remote_build_port is not None:
            self.remote_build_port = cli_remote_build_port
        else:
            env_remote_build_port = os.getenv("ESPHOME_REMOTE_BUILD_PORT", "")
            try:
                self.remote_build_port = (
                    int(env_remote_build_port)
                    if env_remote_build_port
                    else DEFAULT_REMOTE_BUILD_PORT
                )
            except ValueError:
                _LOGGER.warning(
                    "Invalid ESPHOME_REMOTE_BUILD_PORT=%r; falling back to %d",
                    env_remote_build_port,
                    DEFAULT_REMOTE_BUILD_PORT,
                )
                self.remote_build_port = DEFAULT_REMOTE_BUILD_PORT
        # ``--remote-build-host`` (or ``$ESPHOME_REMOTE_BUILD_HOST``).
        # Same precedence pattern as the port: an explicit CLI value
        # wins; absence (or empty / whitespace-only) means "consult
        # the env var, then fall back to the default". Empty-string
        # falls through to the env var rather than passing ``""`` to
        # ``TCPSite`` — aiohttp would translate that to a low-level
        # ``getaddrinfo`` failure with a cryptic error rather than
        # the obvious "0.0.0.0 default". The default is ``0.0.0.0``
        # (all interfaces) — binding the peer-link receiver to the
        # same interface as the HTTP dashboard would break the
        # desktop-app shape, where ``--host 127.0.0.1`` is the
        # dashboard's security boundary but the peer-link still needs
        # to be LAN-reachable so paired peers can actually dial the
        # IPs the mDNS announce broadcasts.
        cli_remote_build_host_raw = getattr(args, "remote_build_host", None)
        cli_remote_build_host = (
            cli_remote_build_host_raw.strip()
            if isinstance(cli_remote_build_host_raw, str)
            else None
        )
        if cli_remote_build_host:
            self.remote_build_host = cli_remote_build_host
        else:
            env_remote_build_host = os.getenv("ESPHOME_REMOTE_BUILD_HOST", "").strip()
            self.remote_build_host = env_remote_build_host or "0.0.0.0"
        self.dev_mode = bool(getattr(args, "dev", False))
        # ``--trusted-domains a,b,c`` (or ``$ESPHOME_TRUSTED_DOMAINS``).
        # Comma-separated. Lower-cased for the case-insensitive match
        # in the WS handshake. Empty list = both Origin and Host
        # allowlists disabled.
        #
        # Precedence: a CLI flag value of ``None`` (argparse default
        # when ``--trusted-domains`` wasn't passed) means "flag not
        # set, consult the env var"; any string value, including the
        # empty string, is an explicit override and wins over the
        # env var. Lets operators say ``--trusted-domains ""`` to
        # disable the checks even when ``$ESPHOME_TRUSTED_DOMAINS``
        # is set in the environment (e.g. inherited from a parent).
        cli_value = getattr(args, "trusted_domains", None)
        raw_trusted = (
            cli_value if cli_value is not None else os.getenv("ESPHOME_TRUSTED_DOMAINS", "")
        )
        self.trusted_domains = [
            host.strip().lower() for host in raw_trusted.split(",") if host.strip()
        ]
        CORE.config_path = self.config_dir / _DASHBOARD_SENTINEL_FILE
        # The long-lived process never fetches external packages in-process:
        # metadata resolution reads cached checkouts, and real builds run in
        # esphome CLI subprocesses with their own CORE. Set once here so the
        # device-scan package merge doesn't git-fetch per repo on every startup.
        CORE.skip_external_update = True

    def rel_path(self, *parts: str) -> Path:
        """
        Return a path relative to the config dir, validated against path traversal.

        ``relative_to`` raises ``ValueError`` when ``parts`` resolve outside
        the config dir; we translate that into a ``CommandError`` so the
        WS dispatcher surfaces it as ``INVALID_ARGS`` instead of the
        generic ``INTERNAL_ERROR`` that an unclassified ``ValueError``
        would produce. Single chokepoint for every handler that builds
        a configuration path.
        """
        joined = self.config_dir.joinpath(*parts)
        assert self.absolute_config_dir is not None  # type narrowing
        try:
            joined.resolve().relative_to(self.absolute_config_dir)
        except ValueError as err:
            # ``!r`` quotes + escapes the offending value so embedded
            # CR/LF/control bytes can't break the error string when
            # the frontend echoes it back to the user. ``!r`` *first*,
            # then truncate, so the bound holds even for control-heavy
            # payloads (a single ``\x00`` repr's to 4 chars, so an
            # 80-byte raw value can otherwise blow past 200 chars).
            display = repr("/".join(parts))
            if len(display) > 100:
                display = f"{display[:97]}..."
            raise CommandError(
                ErrorCode.INVALID_ARGS,
                f"Invalid configuration filename: {display}",
            ) from err
        return joined

    @property
    def status_use_mqtt(self) -> bool:
        return bool(get_bool_env("ESPHOME_DASHBOARD_USE_MQTT"))

    @property
    def create_ingress_site(self) -> bool:
        """Whether to bind the trusted HA Ingress TCP site alongside the public site."""
        if not self.on_ha_addon:
            return False
        # DISABLE_HA_AUTHENTICATION lets operators force ingress users
        # through the password-gated public port too.
        return not get_bool_env("DISABLE_HA_AUTHENTICATION")

    def check_password(self, username: str, password: str) -> bool:
        """
        Verify *username* and *password* in constant time.

        Returns ``False`` when no password is configured — check
        ``using_password`` separately to know whether the gate is active.
        """
        if not self.using_password:
            return False
        username_ok = hmac.compare_digest(username.encode("utf-8"), self.username.encode("utf-8"))
        password_ok = hmac.compare_digest(self.password_hash, hash_password(password))
        return username_ok and password_ok
