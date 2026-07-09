"""Tests for the HA-add-on bind logic in ``DeviceBuilder.run``.

The legacy dashboard had a supervisor ``/auth`` fallback that gated
the public port with HA credentials when ``PASSWORD`` wasn't set;
we don't carry that forward (see issue #85). The add-on is
ingress-only by default. The one way to expose the LAN port is the
operator's explicit, two-part opt-in (legacy parity): the
``leave_front_door_open`` option (``DISABLE_HA_AUTHENTICATION``)
*and* a mapped port 6052 (``--ha-addon-allow-public``), which binds
the public port with no auth at all.

These tests pin the branches:

1. on-ha-addon + no password + ingress (default) → run ingress-only.
2. on-ha-addon + no password + front door open + mapped port → bind
   the public port unauthenticated, keep the ingress site (sidebar).
3. on-ha-addon + no password + only one of the two opt-ins →
   ingress-only with an explanatory warning (never expose unauthed).
4. anything else (password set, not on add-on) → public site as
   normal.
"""

from __future__ import annotations

import builtins
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web

from esphome_device_builder.device_builder import DeviceBuilder

from .conftest import MakeSettingsFactory


def _make_db(
    make_settings: MakeSettingsFactory,
    *,
    on_ha_addon: bool,
    using_password: bool,
    allow_public_port: bool = False,
    unix_socket: str | None = None,
) -> DeviceBuilder:
    """Build a DeviceBuilder with the requested settings shape.

    Tests drive ``front_door_open`` via
    ``monkeypatch.setenv("DISABLE_HA_AUTHENTICATION", ...)`` — the
    same path the production property reads, no class trickery —
    and ``allow_public_port`` directly (the CLI flag the add-on
    passes only when the operator mapped port 6052).
    """
    settings = make_settings()
    settings.on_ha_addon = on_ha_addon
    settings.using_password = using_password
    settings.allow_public_port = allow_public_port
    if using_password:
        settings.username = "admin"
        settings.password_hash = b"x" * 32
    settings.host = "0.0.0.0"
    settings.unix_socket = unix_socket
    settings.port = 6052
    settings.ingress_port = 6053
    settings.ingress_host = ""
    return DeviceBuilder(settings)


def test_ha_addon_no_password_with_ingress_runs_ingress_only(
    make_settings: MakeSettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Public port suppressed; ingress site bound; loud warning logged.

    Drives the ``create_ingress_site`` property via the real
    ``DISABLE_HA_AUTHENTICATION`` env var (unset = ingress is
    available) so the property's actual behaviour is exercised.
    Asserts the operator-facing warning is emitted via ``caplog``
    so a regression that silently suppresses the public-port bind
    surfaces immediately.
    """
    monkeypatch.delenv("DISABLE_HA_AUTHENTICATION", raising=False)
    db = _make_db(make_settings, on_ha_addon=True, using_password=False)

    captured: dict[str, object] = {}

    def fake_run_app(
        app, *, host: list[str], port: int, handle_signals: bool = True, **_: object
    ) -> None:
        captured["host"] = host
        captured["port"] = port
        captured["trusted"] = bool(app.get("trusted_site"))
        captured["handle_signals"] = handle_signals

    with (
        caplog.at_level("WARNING", logger="esphome_device_builder.device_builder"),
        patch("esphome_device_builder.device_builder.web.run_app", fake_run_app),
        patch.object(db, "create_app", wraps=db.create_app) as create_app_spy,
    ):
        db.run()

    # Only the ingress site got bound — public port was suppressed.
    assert captured["port"] == 6053  # ingress_port
    # No ``--ingress-host`` → loopback + supervisor gateway only, NEVER
    # 0.0.0.0: on a host-network add-on all-interfaces would expose the
    # no-auth ingress site on the LAN. Loopback serves HA core's ESPHome
    # integration; the gateway serves the supervisor's ingress proxy.
    assert captured["host"] == ["127.0.0.1", "172.30.32.1"]
    assert captured["trusted"] is True  # trusted=True (auth bypass)
    assert captured["handle_signals"] is False  # we own the stop signal end-to-end

    # The single create_app call was for the trusted ingress, with
    # the ingress-site hook disabled (the app IS the ingress).
    assert create_app_spy.call_count == 1
    kwargs = create_app_spy.call_args.kwargs
    assert kwargs == {"trusted": True, "with_ingress_site": False}

    # The operator-facing safety warning fired. Without this
    # assertion a regression where the bind is suppressed silently
    # would still pass the bind-shape checks above — the loud log
    # is the only signal an operator gets about why their LAN
    # access doesn't work.
    warning_messages = [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelname == "WARNING" and "NOT bound" in rec.getMessage()
    ]
    assert warning_messages, (
        "expected the loud 'Public port ... NOT bound' warning describing "
        "why the public port was suppressed and how to enable it"
    )
    # Pin the addon-specific framing so a future copy edit can't
    # silently regress to the pre-#943 wording that pointed
    # operators at add-on options that don't exist.
    warning = warning_messages[0]
    assert "ingress-only" in warning
    assert "doesn't expose" in warning
    assert "standalone PyPI install" in warning


@pytest.mark.parametrize("unix_socket", [pytest.param(None, id="tcp_socket"), "unix_socket"])
def test_ha_addon_front_door_open_with_mapped_port_binds_public_unauthenticated(
    make_settings: MakeSettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    unix_socket: str | None,
) -> None:
    """Both opt-ins set → public port bound with no auth, ingress kept for the sidebar.

    The explicit two-part opt-in (``DISABLE_HA_AUTHENTICATION`` +
    a mapped port via ``allow_public_port``) must NOT crash; it
    binds ``0.0.0.0:6052`` with the peer guard off so a LAN client
    reaches it (auth is a no-op without a password) but the site
    untrusted so the origin gate stays, logs the wide-open banner,
    and still registers the ingress site so HA-sidebar access
    survives.
    """
    monkeypatch.setenv("DISABLE_HA_AUTHENTICATION", "true")
    db = _make_db(
        make_settings,
        on_ha_addon=True,
        using_password=False,
        allow_public_port=True,
        unix_socket=unix_socket,
    )

    captured: dict[str, object] = {}

    def fake_run_app(
        app, *, host: list[str], port: int, path: str | None = None, **_: object
    ) -> None:
        captured["host"] = host
        captured["port"] = port
        captured["trusted_site"] = bool(app.get("trusted_site"))
        captured["ingress_hook"] = db._start_ingress_site in app.on_startup
        captured["path"] = path

    with (
        caplog.at_level("WARNING", logger="esphome_device_builder.device_builder"),
        patch("esphome_device_builder.device_builder.web.run_app", fake_run_app),
        patch.object(db, "create_app", wraps=db.create_app) as create_app_spy,
    ):
        db.run()

    # Public port bound on all interfaces.
    assert captured["port"] == 6052
    assert captured["host"] == ([] if unix_socket else ["0.0.0.0"])
    assert captured["path"] == unix_socket
    # Not a trusted site: the WS origin/Host gate stays active (auth is a no-op
    # without a password), so a plain cross-origin drive-by is still rejected.
    assert captured["trusted_site"] is False
    # Ingress site still bound alongside it (HA sidebar keeps working).
    assert captured["ingress_hook"] is True

    # The public app drops the peer guard so the LAN reaches it, but stays
    # untrusted to keep the origin gate.
    main_call = create_app_spy.call_args_list[0]
    assert main_call.kwargs == {"trusted": False, "peer_guard": False}

    banner = [r.getMessage() for r in caplog.records if "FRONT DOOR OPEN" in r.getMessage()]
    assert banner, "expected the loud FRONT DOOR OPEN banner"
    assert (unix_socket or "0.0.0.0:6052") in banner[0]
    assert "NO authentication" in banner[0]


def test_ha_addon_front_door_open_without_mapped_port_runs_ingress_only(
    make_settings: MakeSettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Front door open but port not mapped → nothing exposed (legacy parity)."""
    monkeypatch.setenv("DISABLE_HA_AUTHENTICATION", "true")
    db = _make_db(make_settings, on_ha_addon=True, using_password=False, allow_public_port=False)

    captured: dict[str, object] = {}

    def fake_run_app(app, *, host: list[str], port: int, **_: object) -> None:
        captured["host"] = host
        captured["port"] = port

    with (
        caplog.at_level("WARNING", logger="esphome_device_builder.device_builder"),
        patch("esphome_device_builder.device_builder.web.run_app", fake_run_app),
    ):
        db.run()

    # Only the ingress site bound — never 0.0.0.0.
    assert captured["port"] == 6053
    assert captured["host"] == ["127.0.0.1", "172.30.32.1"]
    warnings = [r.getMessage() for r in caplog.records if "NOT bound" in r.getMessage()]
    assert warnings and "not mapped" in warnings[0]


def test_ha_addon_mapped_port_without_front_door_runs_ingress_only(
    make_settings: MakeSettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mapped port without front-door-open → ingress-only; no silent unauthed bind (#85)."""
    monkeypatch.delenv("DISABLE_HA_AUTHENTICATION", raising=False)
    db = _make_db(make_settings, on_ha_addon=True, using_password=False, allow_public_port=True)

    captured: dict[str, object] = {}

    def fake_run_app(app, *, host: list[str], port: int, **_: object) -> None:
        captured["host"] = host
        captured["port"] = port

    with (
        caplog.at_level("WARNING", logger="esphome_device_builder.device_builder"),
        patch("esphome_device_builder.device_builder.web.run_app", fake_run_app),
    ):
        db.run()

    assert captured["port"] == 6053
    assert captured["host"] == ["127.0.0.1", "172.30.32.1"]
    warnings = [r.getMessage() for r in caplog.records if "NOT bound" in r.getMessage()]
    assert warnings and "#85" in warnings[0]


@pytest.mark.parametrize(
    ("on_ha_addon", "disable_env", "allow_public_port", "front", "serve", "ingress"),
    [
        # On the add-on: serve_public needs BOTH opt-ins; create_ingress_site
        # is the inverse of serve_public (so the no-auth banner fires only there).
        (True, False, False, False, False, True),
        (True, False, True, False, False, True),
        (True, True, False, True, False, True),
        (True, True, True, True, True, False),
        # Off the add-on the env var and flag are inert.
        (False, True, True, False, False, False),
    ],
    ids=[
        "addon_default",
        "addon_mapped_no_frontdoor",
        "addon_frontdoor_unmapped",
        "addon_both_optins",
        "off_addon_inert",
    ],
)
def test_front_door_property_truth_table(
    make_settings: MakeSettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
    on_ha_addon: bool,
    disable_env: bool,
    allow_public_port: bool,
    front: bool,
    serve: bool,
    ingress: bool,
) -> None:
    """Pin the security-relevant property matrix so a refactor can't widen exposure."""
    if disable_env:
        monkeypatch.setenv("DISABLE_HA_AUTHENTICATION", "true")
    else:
        monkeypatch.delenv("DISABLE_HA_AUTHENTICATION", raising=False)
    settings = make_settings()
    settings.on_ha_addon = on_ha_addon
    settings.allow_public_port = allow_public_port

    assert settings.front_door_open is front
    assert settings.serve_public_unauthenticated is serve
    assert settings.create_ingress_site is ingress


@pytest.mark.parametrize("unix_socket", [pytest.param(None, id="tcp_socket"), "unix_socket"])
def test_ha_addon_with_password_binds_public_site_normally(
    make_settings: MakeSettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
    unix_socket: str | None,
) -> None:
    """Password set → normal public-site bind, ingress as a hook."""
    monkeypatch.delenv("DISABLE_HA_AUTHENTICATION", raising=False)
    db = _make_db(make_settings, on_ha_addon=True, using_password=True, unix_socket=unix_socket)

    captured: dict[str, object] = {}

    def fake_run_app(
        app, *, host: list[str], port: int, path: str | None = None, **_: object
    ) -> None:
        captured["host"] = host
        captured["port"] = port
        captured["trusted"] = bool(app.get("trusted_site"))
        captured["path"] = path

    with patch("esphome_device_builder.device_builder.web.run_app", fake_run_app):
        db.run()

    # Public port bound (auth gates it via using_password).
    assert captured["port"] == 6052
    assert captured["host"] == ([] if unix_socket else ["0.0.0.0"])
    assert captured["path"] == unix_socket
    assert captured["trusted"] is False


@pytest.mark.parametrize("unix_socket", [pytest.param(None, id="tcp_socket"), "unix_socket"])
def test_non_ha_addon_binds_public_site_normally(
    make_settings: MakeSettingsFactory, unix_socket: str | None
) -> None:
    """Standalone deployment is unaffected by the HA-add-on logic.

    Doesn't need ``monkeypatch`` for ``DISABLE_HA_AUTHENTICATION``:
    when ``on_ha_addon=False`` the property short-circuits and
    returns ``False`` regardless of the env var.
    """
    db = _make_db(make_settings, on_ha_addon=False, using_password=False, unix_socket=unix_socket)

    captured: dict[str, object] = {}

    def fake_run_app(
        app,
        *,
        host: list[str],
        port: int,
        path: str | None = None,
        handle_signals: bool = True,
        **_: object,
    ) -> None:
        captured["host"] = host
        captured["port"] = port
        captured["handle_signals"] = handle_signals
        captured["path"] = path

    with patch("esphome_device_builder.device_builder.web.run_app", fake_run_app):
        db.run()

    # Public port bound — non-add-on deployments get the legacy
    # default of "no auth required, user opts in via PASSWORD".
    assert captured["port"] == 6052
    assert captured["host"] == ([] if unix_socket else ["0.0.0.0"])
    assert captured["path"] == unix_socket
    # We own the stop signal end-to-end (see DeviceBuilder.run); aiohttp
    # must not install its own handler.
    assert captured["handle_signals"] is False


def test_public_run_refuses_port_zero_with_multi_host(
    make_settings: MakeSettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--port 0`` paired with a multi-address NIC refuses the public bind.

    Each ``TCPSite(port=0)`` gets its own OS-assigned port, so the
    advertised ``settings.port`` (mDNS SRV) no longer matches any
    listener. The ingress/remote-build paths already refuse this;
    the public path must too.
    """
    db = _make_db(make_settings, on_ha_addon=False, using_password=False)
    db.settings.host = "eth0"
    db.settings.port = 0
    monkeypatch.setattr(
        "esphome_device_builder.device_builder.resolve_bind_host",
        lambda _: ["192.168.1.10", "192.168.1.11"],
    )

    with (
        patch("esphome_device_builder.device_builder.web.run_app") as run_app_mock,
        pytest.raises(RuntimeError, match=r"--port 0 .* multiple addresses"),
    ):
        db.run()

    run_app_mock.assert_not_called()


def test_ingress_only_run_refuses_port_zero_with_multi_host(
    make_settings: MakeSettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--ingress-port 0`` + multi-address NIC refuses the ingress-only bind."""
    monkeypatch.delenv("DISABLE_HA_AUTHENTICATION", raising=False)
    db = _make_db(make_settings, on_ha_addon=True, using_password=False)
    db.settings.ingress_port = 0
    monkeypatch.setattr(
        "esphome_device_builder.device_builder.resolve_bind_host",
        lambda _: ["192.168.1.10", "192.168.1.11"],
    )

    with (
        patch("esphome_device_builder.device_builder.web.run_app") as run_app_mock,
        pytest.raises(RuntimeError, match=r"--ingress-port 0 .* multiple addresses"),
    ):
        db.run()

    run_app_mock.assert_not_called()


async def test_start_and_stop_ingress_site_lifecycle(make_settings: MakeSettingsFactory) -> None:
    """``_start_ingress_site`` / ``_stop_ingress_site`` actually bind+release.

    Drives the lifecycle hooks directly (rather than running the
    full ``web.run_app``) so the ingress-only path's child app
    construction, runner setup, port bind, and clean cleanup are
    all exercised. Uses port=0 so the OS picks a free ephemeral
    port — avoids flakes when 6053 is already in use on the
    runner.
    """
    db = _make_db(make_settings, on_ha_addon=True, using_password=True)
    db.settings.ingress_port = 0  # let OS pick a free port
    # Single explicit host so the ephemeral port (0) is allowed — the default
    # bind is multi-host (loopback + gateway), which can't share an OS-assigned
    # ephemeral port.
    db.settings.ingress_host = "127.0.0.1"

    # _start_ingress_site reads self.settings.ingress_port via
    # web.TCPSite — a real socket bind. The hook also calls
    # self.create_app(trusted=True, with_lifecycle=False) to build
    # the inner app; that path is what we're trying to cover.
    fake_app: object = object()
    await db._start_ingress_site(fake_app)  # type: ignore[arg-type]
    assert db._ingress_runner is not None

    # And shutting it down releases the bind.
    await db._stop_ingress_site(fake_app)  # type: ignore[arg-type]
    assert db._ingress_runner is None


async def test_start_ingress_site_cleans_up_runner_on_partial_bind_failure(
    make_settings: MakeSettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partial multi-host bind failure releases the half-bound runner."""
    db = _make_db(make_settings, on_ha_addon=True, using_password=True)
    # Fixed port + the default multi-host bind (loopback + supervisor gateway):
    # the first host binds, the second is forced to fail below.
    db.settings.ingress_port = 6053

    real_start = web.TCPSite.start
    calls: list[web.TCPSite] = []

    async def flaky_start(self: web.TCPSite) -> None:
        calls.append(self)
        if len(calls) == 1:
            await real_start(self)
            return
        raise OSError("simulated second-bind failure")

    cleanup_calls: list[web.AppRunner] = []
    real_cleanup = web.AppRunner.cleanup

    async def tracking_cleanup(self: web.AppRunner) -> None:
        cleanup_calls.append(self)
        await real_cleanup(self)

    monkeypatch.setattr(web.TCPSite, "start", flaky_start)
    monkeypatch.setattr(web.AppRunner, "cleanup", tracking_cleanup)

    fake_app: object = object()
    with pytest.raises(OSError, match="simulated second-bind failure"):
        await db._start_ingress_site(fake_app)  # type: ignore[arg-type]

    # Runner attribute never assigned; the half-bound runner was
    # cleaned up inline so ``_stop_ingress_site`` doesn't get a
    # chance to see it.
    assert db._ingress_runner is None
    assert len(calls) == 2, "expected the second bind to be attempted"
    assert len(cleanup_calls) == 1, "expected the half-bound runner to be cleaned up"


async def test_start_ingress_site_refuses_port_zero_with_multi_host(
    make_settings: MakeSettingsFactory,
) -> None:
    """``--ingress-port 0`` with the default multi-host bind refuses to bind."""
    db = _make_db(make_settings, on_ha_addon=True, using_password=True)
    db.settings.ingress_port = 0  # default ingress_host → loopback + gateway (2 hosts)

    fake_app: object = object()
    with pytest.raises(RuntimeError, match=r"--ingress-port 0 .* multiple addresses"):
        await db._start_ingress_site(fake_app)  # type: ignore[arg-type]

    assert db._ingress_runner is None


async def test_on_startup_and_on_cleanup_call_through_to_lifecycle(
    make_settings: MakeSettingsFactory,
) -> None:
    """The aiohttp lifecycle hooks delegate to start()/stop() correctly.

    Exercises the trivial ``_on_startup`` / ``_on_cleanup``
    one-liners by patching ``DeviceBuilder.start`` / ``stop`` and
    asserting they're awaited. Without the patch a full ``start()``
    would spin up controllers, which is heavier than this test
    needs.
    """
    db = _make_db(make_settings, on_ha_addon=False, using_password=False)
    fake_app: object = object()

    with (
        patch.object(db, "start", new=AsyncMock()) as start_mock,
        patch.object(db, "stop", new=AsyncMock()) as stop_mock,
    ):
        await db._on_startup(fake_app)  # type: ignore[arg-type]
        await db._on_cleanup(fake_app)  # type: ignore[arg-type]

    start_mock.assert_awaited_once()
    stop_mock.assert_awaited_once()


def test_get_frontend_dir_returns_none_when_package_missing(
    make_settings: MakeSettingsFactory,
) -> None:
    """Covers the ``ImportError`` fallback in ``_get_frontend_dir``.

    The frontend ships as a separate wheel
    (``esphome-device-builder-frontend``) that's optional —
    without it the dashboard runs in API-only mode. The fallback
    branch returns ``None`` so callers can detect the missing
    package and skip the static-route registration.
    """
    db = _make_db(make_settings, on_ha_addon=False, using_password=False)

    # Force an ImportError by clearing the module from sys.modules
    # and patching the import to raise.
    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "esphome_device_builder_frontend":
            msg = "fake missing"
            raise ImportError(msg)
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    with patch.object(builtins, "__import__", fake_import):
        assert db._get_frontend_dir() is None


def test_create_app_logs_frontend_missing_message(make_settings: MakeSettingsFactory) -> None:
    """``create_app`` logs a friendly hint when the frontend package isn't installed.

    Covers the ``elif with_lifecycle:`` branch that runs when
    ``_get_frontend_dir`` returned ``None``. Without the package
    the dashboard still serves the WS API; the log line tells the
    operator why the UI is missing and how to fix it.
    """
    db = _make_db(make_settings, on_ha_addon=False, using_password=False)

    with (
        patch.object(db, "_get_frontend_dir", return_value=None),
        patch("esphome_device_builder.device_builder._LOGGER") as logger_mock,
    ):
        db.create_app(with_lifecycle=True)

    info_calls = [c for c in logger_mock.info.call_args_list if "Frontend package" in str(c)]
    assert info_calls, "expected the 'Frontend package not installed' log line"
