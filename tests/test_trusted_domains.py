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

These tests exercise three layers:

* The pure ``normalize_host`` / ``host_in_allowlist`` /
  ``origin_in_allowlist`` helpers.
* ``DashboardSettings.parse_args`` — CLI / env-var precedence,
  whitespace handling, the ``--trusted-domains ""`` explicit
  override path.
* End-to-end ``websocket_handler`` integration via
  ``aiohttp_client``: confirms the 403 paths fire on the right
  branches and that a valid Origin / Host pair reaches the WS
  upgrade.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from esphome.core import CORE
from pytest_aiohttp.plugin import AiohttpClient

from esphome_device_builder.api import ws as ws_module
from esphome_device_builder.controllers.config import DashboardSettings
from esphome_device_builder.helpers.origin import (
    host_in_allowlist,
    normalize_host,
    origin_in_allowlist,
)

# ---------------------------------------------------------------------------
# normalize_host
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
        # IP literals canonicalise: expanded / zero-padded spellings
        # collapse to the same compressed form as the allowlist entry.
        ("[2001:0db8:0:0:0:0:0:1]:6052", "2001:db8::1"),
        ("2001:0db8::1", "2001:db8::1"),
        ("[2001:0DB8::0001]:443", "2001:db8::1"),
        # RFC-1123 numeric hostname stays a hostname — the str form of
        # ipaddress.ip_address rejects integer notation, so it is not
        # coerced to a dotted-quad int-IP.
        ("1234", "1234"),
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
    assert normalize_host(raw) == expected


def test_normalize_host_falls_back_on_malformed() -> None:
    """``urlsplit`` of garbage may return empty hostname — fall back to lowercase."""
    # Empty string and lone colon both yield None from .hostname; the
    # fallback returns the lowercase input verbatim so the comparison
    # in host_in_allowlist still has something deterministic.
    assert normalize_host("") == ""
    assert normalize_host("WeirdInput") == "weirdinput"


def test_normalize_host_falls_back_on_invalid_ipv6_url() -> None:
    """An unclosed IPv6 bracket makes ``urlsplit`` raise — falls through to lowercase.

    ``urlsplit("//[invalid")`` raises ``ValueError("Invalid IPv6
    URL")`` rather than returning a parsed result. The
    ``except ValueError`` branch sets ``hostname = None`` so the
    function lands on the bottom fallback (lowercase input
    verbatim). Without it a malformed ``Host`` header would 500
    the WS handshake instead of falling through to the
    allowlist's normal "doesn't match" rejection path.
    """
    assert normalize_host("[invalid") == "[invalid"


# ---------------------------------------------------------------------------
# host_in_allowlist
# ---------------------------------------------------------------------------


def test_host_in_allowlist_empty_means_pass_through() -> None:
    """Empty allowlist = check disabled = always allow.

    The opt-in shape: operators who don't set the env var see no
    behaviour change. Test pins this so a refactor that flips the
    truthiness check (returning False on empty) doesn't break
    every default deployment.
    """
    assert host_in_allowlist("dashboard.local:6052", []) is True


def test_host_in_allowlist_wildcard_match_anything() -> None:
    """``"*"`` is the explicit "match anything" escape hatch."""
    assert host_in_allowlist("anything.example.com", ["*"]) is True
    assert host_in_allowlist("[::1]:6052", ["*"]) is True


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
        # Request Host and allowlist entry spell the same IPv6
        # address differently — canonicalisation makes them match.
        ("[2001:0db8::1]:6052", ["2001:db8::1"]),
        ("[2001:db8::1]:6052", ["2001:0db8:0:0:0:0:0:1"]),
    ],
)
def test_host_in_allowlist_match(host: str, allowlist: list[str]) -> None:
    """Match is case-insensitive, port-tolerant, and bracket-tolerant for IPv6.

    Operators may type ``[::1]`` or ``::1`` — both should match
    a request Host of ``[::1]:6052``. The test catalogues the
    accepted shapes so a normaliser tweak that breaks any of
    them shows up in CI.
    """
    assert host_in_allowlist(host, allowlist) is True


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
    assert host_in_allowlist(host, allowlist) is False


# ---------------------------------------------------------------------------
# origin_in_allowlist
# ---------------------------------------------------------------------------


def test_origin_in_allowlist_empty_means_no_grant() -> None:
    """Empty allowlist + cross-origin = reject (existing strict behaviour).

    Different polarity from the host check — this one only EXTENDS
    acceptance. Empty allowlist falls through to the existing
    Origin-equals-Host hard reject.
    """
    assert origin_in_allowlist("https://dashboard.example.com", []) is False


def test_origin_in_allowlist_wildcard_accepts_any() -> None:
    """``"*"`` accepts any cross-origin connection.

    Escape hatch for operators who want to disable the
    cross-origin restriction entirely (e.g. they trust their
    network and just want the dashboard usable from any
    proxy hostname).
    """
    assert origin_in_allowlist("https://anything.example.com", ["*"]) is True


@pytest.mark.parametrize(
    ("origin", "allowlist"),
    [
        ("https://dashboard.example.com", ["dashboard.example.com"]),
        ("https://Dashboard.Example.com", ["dashboard.example.com"]),
        ("https://dashboard.example.com:8443", ["dashboard.example.com"]),
        ("http://192.168.1.10:6052", ["192.168.1.10"]),
        ("http://[::1]:6052", ["::1"]),
        ("http://[2001:0db8::1]:6052", ["2001:db8::1"]),
    ],
)
def test_origin_in_allowlist_match(origin: str, allowlist: list[str]) -> None:
    """Match is on the Origin URL's hostname (port + scheme stripped)."""
    assert origin_in_allowlist(origin, allowlist) is True


def test_origin_in_allowlist_rejects_unmatched() -> None:
    """An attacker domain that's not in the list stays rejected."""
    assert origin_in_allowlist("https://evil.example.com", ["dashboard.example.com"]) is False


def test_origin_in_allowlist_rejects_malformed() -> None:
    """Garbage Origin header -> reject."""
    # Empty hostname after parsing -> not a useful match candidate.
    assert origin_in_allowlist("not-a-url", ["dashboard.example.com"]) is False


def test_origin_in_allowlist_rejects_invalid_ipv6_url() -> None:
    """An Origin with an unclosed IPv6 bracket makes ``urlparse`` raise.

    ``urlparse("http://[invalid")`` raises ``ValueError("Invalid
    IPv6 URL")`` rather than returning a parsed result. The
    ``except ValueError`` branch turns that into a clean rejection
    so the allowlist check returns ``False`` instead of 500-ing
    the WS handshake on a malformed Origin header.
    """
    assert origin_in_allowlist("http://[invalid", ["dashboard.example.com"]) is False


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
        # ``None`` matches the argparse default — the test mirrors
        # production where ``--trusted-domains`` was not passed and
        # ``parse_args`` should consult ``$ESPHOME_TRUSTED_DOMAINS``.
        "trusted_domains": None,
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


def test_settings_parse_args_disables_external_package_fetch(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``parse_args`` pins ``skip_external_update`` so the scan never git-fetches."""
    monkeypatch.setattr(CORE, "skip_external_update", False)
    DashboardSettings().parse_args(_ns(configuration=str(tmp_path)))
    assert CORE.skip_external_update is True


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


def test_settings_explicit_empty_cli_overrides_env_var(tmp_path: object) -> None:
    """``--trusted-domains ""`` wins over the env var.

    Argparse default is ``None`` (flag not passed); any string
    value, including the empty string, is an explicit override.
    Lets operators disable the checks from the CLI even when
    ``$ESPHOME_TRUSTED_DOMAINS`` is inherited from the parent
    process. Without this, the previous ``getattr(...) or
    os.getenv(...)`` chain treated ``""`` as falsy and silently
    fell back to the env var.
    """
    settings = DashboardSettings()
    with patch.dict(os.environ, {"ESPHOME_TRUSTED_DOMAINS": "from-env.example.com"}):
        settings.parse_args(_ns(configuration=str(tmp_path), trusted_domains=""))
    assert settings.trusted_domains == []


# ---------------------------------------------------------------------------
# websocket_handler integration — exercise the 403 branches end-to-end
# ---------------------------------------------------------------------------


def _public_site_app(trusted_domains: list[str], *, using_password: bool = True) -> web.Application:
    """Build a minimal aiohttp app wired to ``websocket_handler`` on the public site."""
    settings = MagicMock()
    settings.using_password = using_password
    settings.trusted_domains = trusted_domains
    settings.port = 6052
    settings.on_ha_addon = False

    device_builder = MagicMock()
    device_builder.settings = settings
    device_builder.auth = MagicMock()
    device_builder.auth.session_store = MagicMock()

    app = web.Application()
    app["device_builder"] = device_builder
    app["trusted_site"] = False  # public site -> gating runs
    ws_module.init_ws_app(app)
    app.router.add_routes(ws_module.create_ws_routes())
    return app


async def test_handler_rejects_cross_origin_without_allowlist(
    aiohttp_client: AiohttpClient,
) -> None:
    """Origin doesn't match Host, no allowlist → 403 Cross-origin.

    Pin the existing strict behaviour so a refactor of the
    Origin gate doesn't silently remove the protection.
    """
    client = await aiohttp_client(_public_site_app([]))
    resp = await client.get("/ws", headers={"Origin": "https://evil.example.com"})
    assert resp.status == 403
    assert "Cross-origin" in await resp.text()


async def test_handler_accepts_cross_origin_when_origin_in_allowlist(
    aiohttp_client: AiohttpClient,
) -> None:
    """Cross-origin with Origin's hostname in trusted_domains → handshake succeeds.

    Reverse-proxy deployments where Origin is the proxy hostname
    and Host is the upstream bind address. Pinning that the
    handler reaches the WS upgrade (status 101 / TEXT message)
    proves the gate let it through.
    """
    # Allowlist needs ``127.0.0.1`` too because aiohttp_client
    # binds the test server there and the Host-allowlist check
    # runs after the Origin gate; without the IP entry, the test
    # would pass the Origin gate but trip the Host gate.
    client = await aiohttp_client(_public_site_app(["dashboard.example.com", "127.0.0.1"]))
    async with client.ws_connect("/ws", headers={"Origin": "https://dashboard.example.com"}) as ws:
        msg = await ws.receive(timeout=2.0)
        assert msg.type.name in ("TEXT", "BINARY")


async def test_handler_rejects_host_not_in_allowlist(
    aiohttp_client: AiohttpClient,
) -> None:
    """Host header not in allowlist → 403 Host not in trusted-domains.

    DNS-rebinding payload: attacker's hostname resolves to
    victim's LAN IP, browser sends the attacker's Host header,
    operator's allowlist (set to the real dashboard hostname)
    catches it. We pass a matching ``Origin`` so the cross-origin
    gate doesn't short-circuit before the host-allowlist check.
    """
    client = await aiohttp_client(_public_site_app(["dashboard.example.com"]))
    resp = await client.get(
        "/ws",
        headers={
            "Host": "evil.example.com",
            "Origin": "https://evil.example.com",
        },
    )
    assert resp.status == 403
    assert "Host not in trusted-domains" in await resp.text()


async def test_handler_accepts_host_in_allowlist(
    aiohttp_client: AiohttpClient,
) -> None:
    """Host header in allowlist + same-origin → handshake succeeds."""
    client = await aiohttp_client(_public_site_app(["dashboard.example.com"]))
    async with client.ws_connect(
        "/ws",
        headers={
            "Host": "dashboard.example.com",
            "Origin": "https://dashboard.example.com",
        },
    ) as ws:
        msg = await ws.receive(timeout=2.0)
        assert msg.type.name in ("TEXT", "BINARY")


async def test_handler_skips_gating_on_trusted_site(
    aiohttp_client: AiohttpClient,
) -> None:
    """The trusted ingress site bypasses both checks.

    HA Ingress runs upstream auth + bind-network isolation —
    enforcing Origin / Host gating there would block legitimate
    supervisor-routed requests with synthesised headers.
    """
    settings = MagicMock()
    settings.using_password = True
    settings.trusted_domains = ["dashboard.example.com"]
    settings.port = 6052
    settings.on_ha_addon = True

    device_builder = MagicMock()
    device_builder.settings = settings
    device_builder.auth = MagicMock()
    device_builder.auth.session_store = MagicMock()

    app = web.Application()
    app["device_builder"] = device_builder
    app["trusted_site"] = True  # ingress site -> gating skipped
    ws_module.init_ws_app(app)
    app.router.add_routes(ws_module.create_ws_routes())

    client = await aiohttp_client(app)
    async with client.ws_connect(
        "/ws",
        headers={
            "Host": "evil.example.com",
            "Origin": "https://evil.example.com",
        },
    ) as ws:
        msg = await ws.receive(timeout=2.0)
        assert msg.type.name in ("TEXT", "BINARY")


async def test_handler_no_origin_skips_both_gates(
    aiohttp_client: AiohttpClient,
) -> None:
    """Origin-less requests bypass Origin AND Host allowlist gates.

    CLI tools / HA integration / direct ``websockets`` clients
    don't send ``Origin`` (it's a browser-only header). The
    DNS-rebinding attack vector is browser-only by construction
    — a script in evil.com can only re-bind via the browser's
    DNS resolver. Skipping the gates when Origin is absent keeps
    non-browser clients working under a tightened
    ``trusted_domains`` config without weakening the defense.

    Pin both halves: a 403 here would mean an operator who set
    ``trusted_domains`` to harden against rebinding accidentally
    locked their HA integration out.
    """
    client = await aiohttp_client(_public_site_app(["dashboard.example.com"]))
    # No Origin header → CLI-style request. Host is the test
    # client's local IP:port, deliberately NOT in the allowlist.
    async with client.ws_connect("/ws") as ws:
        msg = await ws.receive(timeout=2.0)
        assert msg.type.name in ("TEXT", "BINARY")


async def test_handler_rejects_cross_origin_on_passwordless_public_site(
    aiohttp_client: AiohttpClient,
) -> None:
    """Passwordless public site rejects cross-origin handshakes (no skip-on-passwordless gate)."""
    client = await aiohttp_client(_public_site_app([], using_password=False))
    resp = await client.get("/ws", headers={"Origin": "https://evil.example.com"})
    assert resp.status == 403
    assert "Cross-origin" in await resp.text()


async def test_handler_accepts_same_origin_on_passwordless_public_site(
    aiohttp_client: AiohttpClient,
) -> None:
    """Passwordless same-origin handshake passes — pin the equality branch.

    ``aiohttp.ClientSession.ws_connect`` doesn't auto-set Origin,
    so pass it explicitly; otherwise the test would pass via the
    no-Origin skip and not exercise the same-origin acceptance.
    """
    client = await aiohttp_client(_public_site_app([], using_password=False))
    host = f"{client.host}:{client.port}"
    async with client.ws_connect("/ws", headers={"Origin": f"http://{host}"}) as ws:
        msg = await ws.receive(timeout=2.0)
        assert msg.type.name in ("TEXT", "BINARY")
