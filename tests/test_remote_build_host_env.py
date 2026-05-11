"""
Tests for ``--remote-build-host`` / ``ESPHOME_REMOTE_BUILD_HOST`` resolution.

The peer-link receiver site binds to
:attr:`DashboardSettings.remote_build_host` rather than the
HTTP/WS dashboard's :attr:`~DashboardSettings.host`. The desktop
app shape passes ``--host 127.0.0.1`` (loopback is the dashboard's
security boundary in the Tauri model) but the peer-link still
needs to be LAN-reachable so paired peers can actually dial the
IPs the mDNS announce broadcasts. Default ``0.0.0.0`` is the
right answer because the feature's security gate is Noise +
pre-shared pin, not bind address — operators can still override
to lock the receiver to a specific NIC via the flag or env var.

Precedence mirrors ``--remote-build-port`` (and
``--trusted-domains`` / ``--username``): an explicit CLI value
wins over the env var; a missing / empty / whitespace-only CLI
value falls back to ``$ESPHOME_REMOTE_BUILD_HOST``; an empty /
unset env var falls back to ``0.0.0.0``. Empty / whitespace CLI
values fall through rather than passing ``""`` to ``TCPSite`` —
that would produce a cryptic ``getaddrinfo`` failure instead of
the obvious default.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from esphome_device_builder.controllers.config import DashboardSettings


def _ns(configuration: str, **kwargs: object) -> SimpleNamespace:
    """Minimal argparse-namespace stub for ``DashboardSettings.parse_args``."""
    defaults: dict[str, object] = {
        "ha_addon": False,
        "configuration": configuration,
        "username": "",
        "password": "",
        "log_level": "info",
        "port": 6052,
        "host": "0.0.0.0",
        "ingress_port": 6053,
        "ingress_host": "",
        # ``None`` matches the argparse default — production passes
        # ``None`` when the flag wasn't given so the env-var fallback
        # can fire.
        "remote_build_port": None,
        "remote_build_host": None,
        "dev": False,
        "trusted_domains": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_default_is_all_interfaces(tmp_path: Path) -> None:
    """Neither flag nor env → ``0.0.0.0`` (LAN-reachable)."""
    settings = DashboardSettings()
    # Empty string covers the inherited-but-blank case.
    with patch.dict("os.environ", {"ESPHOME_REMOTE_BUILD_HOST": ""}, clear=False):
        settings.parse_args(_ns(configuration=str(tmp_path)))
    assert settings.remote_build_host == "0.0.0.0"


def test_cli_flag_wins_over_env(tmp_path: Path) -> None:
    """Explicit ``--remote-build-host`` beats the env var."""
    settings = DashboardSettings()
    with patch.dict("os.environ", {"ESPHOME_REMOTE_BUILD_HOST": "10.0.0.5"}, clear=False):
        settings.parse_args(_ns(configuration=str(tmp_path), remote_build_host="192.168.1.10"))
    assert settings.remote_build_host == "192.168.1.10"


def test_env_used_when_cli_unset(tmp_path: Path) -> None:
    """``None`` CLI default falls back to the env var."""
    settings = DashboardSettings()
    with patch.dict("os.environ", {"ESPHOME_REMOTE_BUILD_HOST": "10.0.0.5"}, clear=False):
        settings.parse_args(_ns(configuration=str(tmp_path)))
    assert settings.remote_build_host == "10.0.0.5"


def test_independent_from_http_host(tmp_path: Path) -> None:
    """``--host 127.0.0.1`` does not leak into ``remote_build_host``.

    The desktop-app shape pins ``--host 127.0.0.1`` for its
    loopback security model; the peer-link receiver must still
    default to ``0.0.0.0`` so paired peers on the LAN can actually
    reach the IPs the mDNS announce broadcasts. A regression that
    re-coupled the two would silently re-introduce the
    "advertise points at LAN IP, receiver binds to loopback,
    peers get connection-refused" failure mode the desktop app
    was hitting.
    """
    settings = DashboardSettings()
    with patch.dict("os.environ", {"ESPHOME_REMOTE_BUILD_HOST": ""}, clear=False):
        settings.parse_args(_ns(configuration=str(tmp_path), host="127.0.0.1"))
    assert settings.host == "127.0.0.1"
    assert settings.remote_build_host == "0.0.0.0"


def test_explicit_loopback_override(tmp_path: Path) -> None:
    """Operators who want a loopback peer-link can pin ``--remote-build-host 127.0.0.1``."""
    settings = DashboardSettings()
    with patch.dict("os.environ", {"ESPHOME_REMOTE_BUILD_HOST": ""}, clear=False):
        settings.parse_args(_ns(configuration=str(tmp_path), remote_build_host="127.0.0.1"))
    assert settings.remote_build_host == "127.0.0.1"


def test_empty_cli_flag_falls_through_to_env(tmp_path: Path) -> None:
    """``--remote-build-host ""`` (empty / whitespace) falls back to the env var.

    Passing an empty string straight through to ``TCPSite`` would
    produce a cryptic low-level ``getaddrinfo`` failure rather than
    a clean default — treat an empty / whitespace-only flag as
    "unset" so the env-then-default fallback chain still resolves
    to something bindable.
    """
    settings = DashboardSettings()
    with patch.dict("os.environ", {"ESPHOME_REMOTE_BUILD_HOST": "10.0.0.5"}, clear=False):
        settings.parse_args(_ns(configuration=str(tmp_path), remote_build_host="   "))
    assert settings.remote_build_host == "10.0.0.5"


def test_empty_cli_flag_and_empty_env_falls_through_to_default(tmp_path: Path) -> None:
    """``--remote-build-host ""`` with empty env falls back to ``0.0.0.0``."""
    settings = DashboardSettings()
    with patch.dict("os.environ", {"ESPHOME_REMOTE_BUILD_HOST": ""}, clear=False):
        settings.parse_args(_ns(configuration=str(tmp_path), remote_build_host=""))
    assert settings.remote_build_host == "0.0.0.0"


def test_env_whitespace_falls_through_to_default(tmp_path: Path) -> None:
    """A whitespace-only env value (e.g. ``ESPHOME_REMOTE_BUILD_HOST=" "``) defaults."""
    settings = DashboardSettings()
    with patch.dict("os.environ", {"ESPHOME_REMOTE_BUILD_HOST": "   "}, clear=False):
        settings.parse_args(_ns(configuration=str(tmp_path)))
    assert settings.remote_build_host == "0.0.0.0"
