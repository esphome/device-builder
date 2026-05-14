"""Tests for the fail-secure HA-add-on bind logic in ``DeviceBuilder.run``.

The legacy dashboard had a supervisor ``/auth`` fallback that gated
the public port with HA credentials when ``PASSWORD`` wasn't set;
we don't carry that forward (see issue #85). Without the fallback,
binding the public port without ``USERNAME``/``PASSWORD`` would
leave the dashboard wide-open on the LAN whenever the add-on's
``ports:`` mapping exposed it.

These tests pin the three branches of the fail-secure logic:

1. on-ha-addon + no password + ingress available → run ingress-only.
2. on-ha-addon + no password + ingress disabled → refuse to start.
3. anything else (password set, not on add-on) → public site as
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
) -> DeviceBuilder:
    """Build a DeviceBuilder with the requested settings shape.

    Tests drive ``create_ingress_site`` (the only HA-add-on
    setting that varies between scenarios) via
    ``monkeypatch.setenv("DISABLE_HA_AUTHENTICATION", ...)`` —
    same path the production code reads, no class trickery.
    """
    settings = make_settings()
    settings.on_ha_addon = on_ha_addon
    settings.using_password = using_password
    if using_password:
        settings.username = "admin"
        settings.password_hash = b"x" * 32
    settings.host = "0.0.0.0"
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

    def fake_run_app(app, *, host: list[str], port: int, **_: object) -> None:
        captured["host"] = host
        captured["port"] = port
        captured["trusted"] = bool(app.get("trusted_site"))

    with (
        caplog.at_level("WARNING", logger="esphome_device_builder.device_builder"),
        patch("esphome_device_builder.device_builder.web.run_app", fake_run_app),
        patch.object(db, "create_app", wraps=db.create_app) as create_app_spy,
    ):
        db.run()

    # Only the ingress site got bound — public port was suppressed.
    assert captured["port"] == 6053  # ingress_port
    # ``host`` is now always a list — ``resolve_bind_host`` wraps
    # literals in a singleton so the bind code can fan out
    # uniformly when the operator passes an interface name.
    assert captured["host"] == ["0.0.0.0"]  # ingress_host fallback
    assert captured["trusted"] is True  # trusted=True (auth bypass)

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
    assert "USERNAME and PASSWORD" in warning_messages[0]


def test_ha_addon_no_password_no_ingress_refuses_to_start(
    make_settings: MakeSettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``DISABLE_HA_AUTHENTICATION`` + no password = refuse to start.

    Without ingress and without credentials there's nothing safe
    to bind. Failing loudly at startup is the only correct outcome
    — silently doing nothing would look like a working dashboard
    that just isn't reachable.
    """
    monkeypatch.setenv("DISABLE_HA_AUTHENTICATION", "true")
    db = _make_db(make_settings, on_ha_addon=True, using_password=False)

    with (
        patch("esphome_device_builder.device_builder.web.run_app") as run_app_mock,
        pytest.raises(RuntimeError, match="DISABLE_HA_AUTHENTICATION"),
    ):
        db.run()

    # Nothing bound.
    run_app_mock.assert_not_called()


def test_ha_addon_with_password_binds_public_site_normally(
    make_settings: MakeSettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Password set → normal public-site bind, ingress as a hook."""
    monkeypatch.delenv("DISABLE_HA_AUTHENTICATION", raising=False)
    db = _make_db(make_settings, on_ha_addon=True, using_password=True)

    captured: dict[str, object] = {}

    def fake_run_app(app, *, host: list[str], port: int, **_: object) -> None:
        captured["host"] = host
        captured["port"] = port
        captured["trusted"] = bool(app.get("trusted_site"))

    with patch("esphome_device_builder.device_builder.web.run_app", fake_run_app):
        db.run()

    # Public port bound (auth gates it via using_password).
    assert captured["port"] == 6052
    assert captured["host"] == ["0.0.0.0"]
    assert captured["trusted"] is False


def test_non_ha_addon_binds_public_site_normally(make_settings: MakeSettingsFactory) -> None:
    """Standalone deployment is unaffected by the HA-add-on logic.

    Doesn't need ``monkeypatch`` for ``DISABLE_HA_AUTHENTICATION``:
    when ``on_ha_addon=False`` the property short-circuits and
    returns ``False`` regardless of the env var.
    """
    db = _make_db(make_settings, on_ha_addon=False, using_password=False)

    captured: dict[str, object] = {}

    def fake_run_app(app, *, host: list[str], port: int, **_: object) -> None:
        captured["host"] = host
        captured["port"] = port

    with patch("esphome_device_builder.device_builder.web.run_app", fake_run_app):
        db.run()

    # Public port bound — non-add-on deployments get the legacy
    # default of "no auth required, user opts in via PASSWORD".
    assert captured["port"] == 6052
    assert captured["host"] == ["0.0.0.0"]


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
    db.settings.ingress_port = 6053  # fixed port; multi-host expansion is allowed

    monkeypatch.setattr(
        "esphome_device_builder.device_builder.resolve_bind_host",
        lambda _: ["127.0.0.1", "127.0.0.1"],
    )

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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--ingress-port 0`` paired with a multi-address NIC refuses to bind."""
    db = _make_db(make_settings, on_ha_addon=True, using_password=True)
    db.settings.ingress_port = 0

    monkeypatch.setattr(
        "esphome_device_builder.device_builder.resolve_bind_host",
        lambda _: ["192.168.1.10", "192.168.1.11"],
    )

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
