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

from pathlib import Path
from unittest.mock import patch

import pytest

from esphome_device_builder.controllers.config import DashboardSettings
from esphome_device_builder.device_builder import DeviceBuilder


def _make_db(
    *,
    tmp_path: Path,
    on_ha_addon: bool,
    using_password: bool,
    create_ingress_site: bool,
) -> DeviceBuilder:
    """Build a DeviceBuilder with the requested settings shape.

    The settings ``create_ingress_site`` property is overridden via
    a temporary subclass so the test can drive every combination
    without faking the ``DISABLE_HA_AUTHENTICATION`` env var.
    """
    settings = DashboardSettings()
    settings.config_dir = tmp_path
    settings.absolute_config_dir = tmp_path.resolve()
    settings.on_ha_addon = on_ha_addon
    settings.using_password = using_password
    if using_password:
        settings.username = "admin"
        settings.password_hash = b"x" * 32
    settings.host = "0.0.0.0"
    settings.port = 6052
    settings.ingress_port = 6053
    settings.ingress_host = ""

    class _SettingsOverride(DashboardSettings):
        @property
        def create_ingress_site(self) -> bool:  # type: ignore[override]
            return create_ingress_site

    settings.__class__ = _SettingsOverride
    return DeviceBuilder(settings)


def test_ha_addon_no_password_with_ingress_runs_ingress_only(tmp_path: Path) -> None:
    """Public port suppressed; ingress site bound; loud warning logged."""
    db = _make_db(
        tmp_path=tmp_path,
        on_ha_addon=True,
        using_password=False,
        create_ingress_site=True,
    )

    captured: dict[str, object] = {}

    def fake_run_app(app, *, host: str, port: int) -> None:
        captured["host"] = host
        captured["port"] = port
        captured["trusted"] = bool(app.get("trusted_site"))

    with (
        patch("esphome_device_builder.device_builder.web.run_app", fake_run_app),
        patch.object(db, "create_app", wraps=db.create_app) as create_app_spy,
    ):
        db.run()

    # Only the ingress site got bound — public port was suppressed.
    assert captured["port"] == 6053  # ingress_port
    assert captured["host"] == "0.0.0.0"  # ingress_host fallback
    assert captured["trusted"] is True  # trusted=True (auth bypass)

    # The single create_app call was for the trusted ingress, with
    # the ingress-site hook disabled (the app IS the ingress).
    assert create_app_spy.call_count == 1
    kwargs = create_app_spy.call_args.kwargs
    assert kwargs == {"trusted": True, "with_ingress_site": False}


def test_ha_addon_no_password_no_ingress_refuses_to_start(tmp_path: Path) -> None:
    """``DISABLE_HA_AUTHENTICATION`` + no password = refuse to start.

    Without ingress and without credentials there's nothing safe
    to bind. Failing loudly at startup is the only correct outcome
    — silently doing nothing would look like a working dashboard
    that just isn't reachable.
    """
    db = _make_db(
        tmp_path=tmp_path,
        on_ha_addon=True,
        using_password=False,
        create_ingress_site=False,
    )

    with (
        patch("esphome_device_builder.device_builder.web.run_app") as run_app_mock,
        pytest.raises(RuntimeError, match="DISABLE_HA_AUTHENTICATION"),
    ):
        db.run()

    # Nothing bound.
    run_app_mock.assert_not_called()


def test_ha_addon_with_password_binds_public_site_normally(tmp_path: Path) -> None:
    """Password set → normal public-site bind, ingress as a hook."""
    db = _make_db(
        tmp_path=tmp_path,
        on_ha_addon=True,
        using_password=True,
        create_ingress_site=True,
    )

    captured: dict[str, object] = {}

    def fake_run_app(app, *, host: str, port: int) -> None:
        captured["host"] = host
        captured["port"] = port
        captured["trusted"] = bool(app.get("trusted_site"))

    with patch("esphome_device_builder.device_builder.web.run_app", fake_run_app):
        db.run()

    # Public port bound (auth gates it via using_password).
    assert captured["port"] == 6052
    assert captured["host"] == "0.0.0.0"
    assert captured["trusted"] is False


def test_non_ha_addon_binds_public_site_normally(tmp_path: Path) -> None:
    """Standalone deployment is unaffected by the HA-add-on logic."""
    db = _make_db(
        tmp_path=tmp_path,
        on_ha_addon=False,
        using_password=False,
        create_ingress_site=False,
    )

    captured: dict[str, object] = {}

    def fake_run_app(app, *, host: str, port: int) -> None:
        captured["host"] = host
        captured["port"] = port

    with patch("esphome_device_builder.device_builder.web.run_app", fake_run_app):
        db.run()

    # Public port bound — non-add-on deployments get the legacy
    # default of "no auth required, user opts in via PASSWORD".
    assert captured["port"] == 6052
    assert captured["host"] == "0.0.0.0"
