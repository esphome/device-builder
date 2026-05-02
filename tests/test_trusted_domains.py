"""Tests for the ``ESPHOME_TRUSTED_DOMAINS`` allowlist on the WS handshake.

Single allowlist (``DashboardSettings.trusted_domains``) drives two
checks in ``api/ws.py``:

* **Origin allowlist** — when the browser's ``Origin`` doesn't
  equal ``Host`` (reverse-proxy deployments where the proxy
  hostname differs from the upstream bind address), accept the
  cross-origin handshake if Origin's hostname is in the allowlist.
* **Host allowlist** — reject any handshake whose ``Host`` header
  isn't in the allowlist. Defense in depth against DNS rebinding,
  on top of the existing ``auth/login`` gate.

These tests exercise the pure helpers and the parsing on
``DashboardSettings``. Full WS-handshake integration is covered by
the existing aiohttp-client tests.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from esphome_device_builder.api.ws import (
    _host_in_allowlist,
    _normalize_host,
    _origin_in_allowlist,
)
from esphome_device_builder.controllers.config import DashboardSettings

# ---------------------------------------------------------------------------
# _normalize_host
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("dashboard.local", "dashboard.local"),
        ("Dashboard.Local", "dashboard.local"),
        ("dashboard.local:6052", "dashboard.local"),
        ("DASHBOARD.LOCAL:6052", "dashboard.local"),
        ("192.168.1.10", "192.168.1.10"),
        ("192.168.1.10:6052", "192.168.1.10"),
        ("[::1]", "::1"),
        ("[::1]:6052", "::1"),
        ("[FE80::1]:6052", "fe80::1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
    ],
)
def test_normalize_host_strips_port_and_brackets(raw: str, expected: str) -> None:
    """Lower-case, port-stripped, IPv6 brackets stripped.

    HTTP Host headers carry IPv6 in brackets (``[::1]:6052``) so a
    naive ``split(":", 1)[0]`` would chop off the first segment of
    the address. ``urlsplit`` handles both shapes (IPv4/hostname
    plus port, ``[ipv6]`` plus port) and returns the unbracketed
    hostname; this test pins both branches.
    """
    assert _normalize_host(raw) == expected


def test_normalize_host_falls_back_on_malformed() -> None:
    """``urlsplit`` of garbage may return empty hostname — fall back to lowercase."""
    # Empty string and lone colon both yield None from .hostname; the
    # fallback returns the lowercase input verbatim so the comparison
    # in _host_in_allowlist still has something deterministic.
    assert _normalize_host("") == ""
    assert _normalize_host("WeirdInput") == "weirdinput"


# ---------------------------------------------------------------------------
# _host_in_allowlist
# ---------------------------------------------------------------------------


def test_host_in_allowlist_empty_means_pass_through() -> None:
    """Empty allowlist = check disabled = always allow.

    The opt-in shape: operators who don't set the env var see no
    behaviour change. Test pins this so a refactor that flips the
    truthiness check (returning False on empty) doesn't break
    every default deployment.
    """
    assert _host_in_allowlist("dashboard.local:6052", []) is True


def test_host_in_allowlist_wildcard_match_anything() -> None:
    """``"*"`` is the explicit "match anything" escape hatch."""
    assert _host_in_allowlist("anything.example.com", ["*"]) is True
    assert _host_in_allowlist("[::1]:6052", ["*"]) is True


@pytest.mark.parametrize(
    ("host", "allowlist"),
    [
        ("dashboard.local:6052", ["dashboard.local"]),
        ("Dashboard.Local:6052", ["dashboard.local"]),
        ("dashboard.local", ["DASHBOARD.LOCAL"]),
        ("192.168.1.10:6052", ["192.168.1.10"]),
        ("[::1]:6052", ["::1"]),
        ("[::1]:6052", ["[::1]"]),
        ("[fe80::1]:6052", ["FE80::1"]),
    ],
)
def test_host_in_allowlist_match(host: str, allowlist: list[str]) -> None:
    """Match is case-insensitive, port-tolerant, and bracket-tolerant for IPv6.

    Operators may type ``[::1]`` or ``::1`` — both should match
    a request Host of ``[::1]:6052``. The test catalogues the
    accepted shapes so a normaliser tweak that breaks any of
    them shows up in CI.
    """
    assert _host_in_allowlist(host, allowlist) is True


@pytest.mark.parametrize(
    ("host", "allowlist"),
    [
        ("evil.example.com:6052", ["dashboard.local"]),
        ("dashboard.example.com", ["dashboard.local"]),
        ("192.168.1.20:6052", ["192.168.1.10"]),
    ],
)
def test_host_in_allowlist_reject_non_match(host: str, allowlist: list[str]) -> None:
    """Anything not in the allowlist is rejected.

    DNS-rebinding payload would land here: attacker's hostname
    resolves to victim's LAN IP, browser sends Host header for
    the attacker domain, the allowlist (set to the operator's
    real domain) catches it.
    """
    assert _host_in_allowlist(host, allowlist) is False


# ---------------------------------------------------------------------------
# _origin_in_allowlist
# ---------------------------------------------------------------------------


def test_origin_in_allowlist_empty_means_no_grant() -> None:
    """Empty allowlist + cross-origin = reject (existing strict behaviour).

    Different polarity from the host check — this one only EXTENDS
    acceptance. Empty allowlist falls through to the existing
    Origin-equals-Host hard reject.
    """
    assert _origin_in_allowlist("https://dashboard.example.com", []) is False


def test_origin_in_allowlist_wildcard_accepts_any() -> None:
    """``"*"`` accepts any cross-origin connection.

    Escape hatch for operators who want to disable the
    cross-origin restriction entirely (e.g. they trust their
    network and just want the dashboard usable from any
    proxy hostname).
    """
    assert _origin_in_allowlist("https://anything.example.com", ["*"]) is True


@pytest.mark.parametrize(
    ("origin", "allowlist"),
    [
        ("https://dashboard.example.com", ["dashboard.example.com"]),
        ("https://Dashboard.Example.com", ["dashboard.example.com"]),
        ("https://dashboard.example.com:8443", ["dashboard.example.com"]),
        ("http://192.168.1.10:6052", ["192.168.1.10"]),
        ("http://[::1]:6052", ["::1"]),
    ],
)
def test_origin_in_allowlist_match(origin: str, allowlist: list[str]) -> None:
    """Match is on the Origin URL's hostname (port + scheme stripped)."""
    assert _origin_in_allowlist(origin, allowlist) is True


def test_origin_in_allowlist_rejects_unmatched() -> None:
    """An attacker domain that's not in the list stays rejected."""
    assert _origin_in_allowlist("https://evil.example.com", ["dashboard.example.com"]) is False


def test_origin_in_allowlist_rejects_malformed() -> None:
    """Garbage Origin header -> reject."""
    # Empty hostname after parsing -> not a useful match candidate.
    assert _origin_in_allowlist("not-a-url", ["dashboard.example.com"]) is False


# ---------------------------------------------------------------------------
# DashboardSettings.parse_args
# ---------------------------------------------------------------------------


def _ns(configuration: str, **kwargs: object) -> SimpleNamespace:
    """Minimal argparse-namespace stub for ``DashboardSettings.parse_args``.

    Caller supplies ``configuration`` (always a ``tmp_path``-derived
    path); defaults stand in for the rest of the argparse Namespace
    so the parse code path doesn't need to special-case missing
    attributes.
    """
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
        "dev": False,
        "trusted_domains": "",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_settings_parses_cli_flag(tmp_path: object) -> None:
    """``--trusted-domains a,b,c`` lands in the dataclass field, lower-cased."""
    settings = DashboardSettings()
    settings.parse_args(
        _ns(
            configuration=str(tmp_path),
            trusted_domains="Dashboard.Local,192.168.1.10",
        )
    )
    assert settings.trusted_domains == ["dashboard.local", "192.168.1.10"]


def test_settings_parses_env_var_when_flag_unset(tmp_path: object) -> None:
    """``$ESPHOME_TRUSTED_DOMAINS`` is the legacy-compat fallback.

    Matches the upstream ESPHome dashboard's env var name so
    operators migrating from the legacy dashboard don't have to
    learn a new knob.
    """
    settings = DashboardSettings()
    with patch.dict(
        os.environ,
        {"ESPHOME_TRUSTED_DOMAINS": "dashboard.example.com,proxy.example.com"},
    ):
        settings.parse_args(_ns(configuration=str(tmp_path)))
    assert settings.trusted_domains == [
        "dashboard.example.com",
        "proxy.example.com",
    ]


def test_settings_cli_flag_wins_over_env_var(tmp_path: object) -> None:
    """When both are set, the CLI flag wins.

    Standard precedence — explicit CLI overrides the inherited
    environment.
    """
    settings = DashboardSettings()
    with patch.dict(os.environ, {"ESPHOME_TRUSTED_DOMAINS": "from-env.example.com"}):
        settings.parse_args(
            _ns(configuration=str(tmp_path), trusted_domains="from-cli.example.com")
        )
    assert settings.trusted_domains == ["from-cli.example.com"]


def test_settings_strips_whitespace_and_blanks(tmp_path: object) -> None:
    """Trailing commas / spaces don't produce empty list entries.

    Operators copy-pasting from docs occasionally leave
    ``"a, b,, c, "`` — make the parser tolerant. Empty entries
    in the allowlist would silently match a Host header of
    ``""`` (the empty string normalises to itself), which would
    be a real bug.
    """
    settings = DashboardSettings()
    settings.parse_args(_ns(configuration=str(tmp_path), trusted_domains="  a,, b,c,   "))
    assert settings.trusted_domains == ["a", "b", "c"]


def test_settings_empty_when_neither_set(tmp_path: object) -> None:
    """Default = empty list = checks disabled.

    Backwards-compatible: existing deployments don't suddenly
    start rejecting their own Host headers.
    """
    settings = DashboardSettings()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("ESPHOME_TRUSTED_DOMAINS", None)
        settings.parse_args(_ns(configuration=str(tmp_path)))
    assert settings.trusted_domains == []
