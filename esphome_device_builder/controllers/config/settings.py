"""Dashboard settings parsed from CLI args and environment."""

from __future__ import annotations

import hmac
import ipaddress
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from esphome.core import CORE
from esphome.helpers import get_bool_env
from esphome.helpers import write_file as atomic_write_file

from ...constants import (
    DEFAULT_INGRESS_PORT,
    DEFAULT_REMOTE_BUILD_PORT,
    HA_INGRESS_DEFAULT_BIND_HOSTS,
    SECRETS_FILENAME,
)
from ...helpers.api import CommandError
from ...helpers.auth import hash_password
from ...helpers.credentials import resolve_credentials
from ...helpers.network_interfaces import resolve_bind_host
from ...helpers.secrets_state import migrate_placeholder_wifi_secrets
from ...models import ErrorCode

_LOGGER = logging.getLogger(__name__)

_DASHBOARD_SENTINEL_FILE = "___DASHBOARD_SENTINEL___.yaml"


def normalize_pairing_sources(raw: str) -> list[str]:
    """
    Parse ``--allow-pairing-source`` into a normalised source-IP list.

    Splits on commas, normalises each entry through :mod:`ipaddress`
    so the ``pair_flow`` compare is wire-form-agnostic (``::1`` vs
    ``0:0:0:0:0:0:0:1``), and silently drops unparseable entries —
    format is validated loudly at the parser, this is just the
    lossless normalisation both the parser and settings share.
    Preserves order, deduplicates.
    """
    out: list[str] = []
    for entry in raw.split(","):
        candidate = entry.strip()
        if not candidate:
            continue
        try:
            normalized = str(ipaddress.ip_address(candidate))
        except ValueError:
            continue
        if normalized not in out:
            out.append(normalized)
    return out


# Upper bound on the ESPHome Desktop wrapper version string; a value past this
# is treated as unset rather than rendered in the footer.
_MAX_DESKTOP_VERSION_LEN = 64

# Upper bound on the ESPHome Desktop CLI path; generous enough for real bundle
# paths but rejects an absurd env value. A value past this (or non-printable) is
# treated as unset so `desktop_update_capable` stays false.
_MAX_DESKTOP_BIN_LEN = 4096


@dataclass
class DashboardSettings:
    """Application settings parsed from CLI args and environment."""

    config_dir: Path = field(default_factory=Path)
    absolute_config_dir: Path | None = None
    username: str = ""
    password_hash: bytes = field(default_factory=bytes)
    using_password: bool = False
    on_ha_addon: bool = False
    allow_public_port: bool = False
    log_level: str = "info"
    port: int = 6052
    host: str = "0.0.0.0"
    unix_socket: str | None = None
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
    # Headless remote-build server mode (``--remote-build-only``): no
    # HTTP dashboard is served and the peer-link listener binds
    # regardless of the persisted ``RemoteBuildSettings.enabled``
    # toggle — with no UI to flip the toggle, an on-disk ``False``
    # would otherwise brick the mode. Pairing bootstrap (15-minute
    # key-gated auto-approve window, console fingerprint banner) lives
    # in ``_remote_build_only.py``.
    remote_build_only: bool = False
    # Optional source-IP allowlist for the ``--remote-build-only``
    # first-pair auto-approve window. When non-empty, the bootstrap
    # only auto-approves a ``pair_request`` whose peer IP is in this
    # set — closing the "any LAN peer who times the window wins the
    # pairing" vector bdraco flagged (the residual accepted risk when
    # unset is documented in docs/THREAT_MODEL.md). Entries are
    # normalised through :mod:`ipaddress` so ``192.168.1.5`` matches
    # regardless of the wire form. Empty (the default) keeps the
    # trust-on-first-use behaviour.
    allow_pairing_sources: list[str] = field(default_factory=list)
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
        self.allow_public_port = getattr(args, "ha_addon_allow_public", False)
        # Credentials resolve through ``resolve_credentials``: CLI flag, then
        # ``$ESPHOME_*``, then the deprecated bare ``$USERNAME`` / ``$PASSWORD``
        # pair (kept for back-compat with pre-rename dashboards, warned about at
        # startup). The bare pair is adopted only when ``$PASSWORD`` is set, so
        # the OS-provided ``$USERNAME`` is never read on its own.
        resolved = resolve_credentials(
            getattr(args, "username", "") or "",
            getattr(args, "password", "") or "",
        )
        self.username = resolved.username
        self.using_password = bool(resolved.username and resolved.password)
        if self.using_password:
            self.password_hash = hash_password(resolved.password)
        self.config_dir = Path(args.configuration)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.absolute_config_dir = self.config_dir.resolve()
        # Ensure secrets.yaml exists (ESPHome fails if !secret references
        # can't find it, and the Secrets editor expects a real file).
        # Atomic write — a crash mid-write would leave the user with a
        # half-bootstrap'd secrets file and the next startup would see
        # ``not exists() == False`` on the partial and skip this branch,
        # leaving them stuck. ``write_file`` stages in a sibling tempfile +
        # ``shutil.move`` so the file is either fully there or not at all.
        #
        # No Wi-Fi placeholders are seeded: Wi-Fi credentials are collected
        # per-device in the create wizard (which writes them here via
        # ``config/set_wifi_credentials``), and generation is adaptive — a
        # device created before any Wi-Fi secret exists gets a no-network
        # stub rather than a broken ``!secret wifi_ssid``.
        secrets_path = self.config_dir / SECRETS_FILENAME
        if not secrets_path.exists():
            atomic_write_file(
                secrets_path,
                "# Secrets — referenced from device configs via !secret\n"
                "# Add Wi-Fi credentials here, or let the create-device\n"
                "# wizard add them for you.\n",
            )
        else:
            # Existing install: drop any leftover seeded Wi-Fi placeholders so a
            # no-ssid create doesn't emit a !secret pointing at the placeholder
            # (compiles, never joins). No-op once the user has set real values.
            migrate_placeholder_wifi_secrets(self.config_dir)
        self.log_level = getattr(args, "log_level", "info")
        self.port = getattr(args, "port", 6052)
        self.host = getattr(args, "host", "0.0.0.0")
        self.unix_socket = getattr(args, "socket", None)
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
        self.remote_build_only = bool(getattr(args, "remote_build_only", False))
        # ``--allow-pairing-source a,b`` — comma-separated source IPs,
        # normalised through ``ipaddress`` so the compare in
        # ``pair_flow`` is wire-form-agnostic. Format is validated at the
        # parser (``__main__._validate_mode_flags``); any entry that
        # slips through unparseable is dropped rather than raising here.
        self.allow_pairing_sources = normalize_pairing_sources(
            getattr(args, "allow_pairing_source", "") or ""
        )
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
    def desktop_version(self) -> str:
        """ESPHome Desktop wrapper version from the env; '' when unset or unusable."""
        raw = os.getenv("ESPHOME_DESKTOP_VERSION", "").strip()
        if not raw or len(raw) > _MAX_DESKTOP_VERSION_LEN or not raw.isprintable():
            return ""
        return raw

    @property
    def desktop_bin(self) -> str:
        """Path to the ESPHome Desktop ``esphome-desktop`` CLI, from the env.

        Only ESPHome Desktop 0.14.0+ exports ``ESPHOME_DESKTOP_BIN``; older
        apps set ``ESPHOME_DESKTOP_VERSION`` but not this, so an empty value
        means "no update `api` available" even when ``desktop_version`` is set.

        Sanitized like ``desktop_version``: a blank, non-printable (control
        chars / newlines, which would also be a log-injection vector), or
        absurdly long value is treated as unset.
        """
        raw = os.getenv("ESPHOME_DESKTOP_BIN", "").strip()
        if not raw or len(raw) > _MAX_DESKTOP_BIN_LEN or not raw.isprintable():
            return ""
        return raw

    @property
    def desktop_update_capable(self) -> bool:
        """Whether the desktop app exposes its update ``api`` (0.14.0+).

        Derived from ``desktop_bin`` presence, not ``desktop_version``, so the
        "Check for updates" UI stays hidden on older desktop apps that predate
        the CLI.
        """
        return bool(self.desktop_bin)

    @property
    def front_door_open(self) -> bool:
        """Operator disabled external auth (legacy leave_front_door_open env var)."""
        return self.on_ha_addon and get_bool_env("DISABLE_HA_AUTHENTICATION")

    @property
    def serve_public_unauthenticated(self) -> bool:
        """
        Bind the public LAN port with no auth at all.

        Requires both the front-door-open opt-in *and* the operator having
        mapped port 6052 (``--ha-addon-allow-public``); legacy parity needed
        both, and the add-on is host-network with no nginx, so the bind is the
        LAN exposure.
        """
        return self.front_door_open and self.allow_public_port

    @property
    def create_ingress_site(self) -> bool:
        """
        Whether the trusted HA Ingress site is the add-on's auth boundary.

        True for every add-on shape except the deliberately wide-open one
        (front door open + mapped port), where the public port carries no auth
        and the unprotected-startup banner must fire.
        """
        return self.on_ha_addon and not self.serve_public_unauthenticated

    @property
    def ingress_bind_hosts(self) -> list[str]:
        """
        Bind targets for the trusted (no-auth) HA Ingress site.

        Defaults to loopback + the supervisor gateway, never all interfaces:
        ``0.0.0.0`` on a host-network add-on would expose the no-auth site on
        the LAN. An explicit ``--ingress-host`` overrides the bind (IP or NIC
        name), but ``ingress_peer_guard`` still restricts sources to loopback
        and the supervisor regardless, so an override alone can't reopen it.
        """
        if self.ingress_host:
            return resolve_bind_host(self.ingress_host)
        return list(HA_INGRESS_DEFAULT_BIND_HOSTS)

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
