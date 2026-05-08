"""
Tests for the dashboard's own ``_esphomebuilder._tcp.local.`` mDNS advertise.

Covers the helper in isolation (TXT shape, default name / hostname
derivation, idempotent register / unregister, fail-soft on zeroconf
errors) plus the wiring through ``DeviceBuilder.start()`` /
``stop()`` (advertise registers when zeroconf is up, skips when
zeroconf is ``None``, unregisters before the responder is closed).

The helper level uses an ``AsyncMock`` for ``async_register_service``
/ ``async_unregister_service`` so we can assert call counts and
inspect the ``ServiceInfo`` argument without standing up a real
multicast listener; the integration test uses the same
``_hermetic_lifecycle`` fixture as ``test_device_builder_lifecycle.py``.
"""

from __future__ import annotations

import socket
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder import device_builder as db_module
from esphome_device_builder.controllers._device_state_monitor import DeviceStateMonitor
from esphome_device_builder.device_builder import DeviceBuilder
from esphome_device_builder.helpers.dashboard_advertise import (
    SERVICE_TYPE,
    DashboardAdvertiser,
    _default_friendly_name,
    _default_hostname,
)


def _make_advertiser(
    *,
    name: str | None = None,
    hostname: str | None = None,
    port: int = 6052,
) -> DashboardAdvertiser:
    return DashboardAdvertiser(
        port=port,
        server_version="1.2.3",
        esphome_version="2026.5.0",
        name=name,
        hostname=hostname,
    )


# ---------------------------------------------------------------------------
# Default-name helpers
# ---------------------------------------------------------------------------


def test_default_friendly_name_strips_dotted_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mac-style ``desktop.local`` from gethostname yields ``desktop``."""
    monkeypatch.setattr(socket, "gethostname", lambda: "desktop.local")
    assert _default_friendly_name() == "desktop"


def test_default_friendly_name_falls_back_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty/whitespace hostname falls back to a stable string."""
    monkeypatch.setattr(socket, "gethostname", lambda: "")
    assert _default_friendly_name() == "esphome-dashboard"


def test_default_hostname_appends_local_when_no_dot(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare ``desktop`` becomes ``desktop.local`` for the TXT field."""
    monkeypatch.setattr(socket, "gethostname", lambda: "desktop")
    assert _default_hostname() == "desktop.local"


def test_default_hostname_keeps_dotted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hostname with a dot is trusted as already-FQDN-ish."""
    monkeypatch.setattr(socket, "gethostname", lambda: "desktop.lan")
    assert _default_hostname() == "desktop.lan"


def test_default_hostname_does_not_use_getfqdn(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    The default-hostname path must NOT route through ``socket.getfqdn``.

    On macOS that resolver can return the reverse-DNS arpa form (e.g.
    a long ``...ip6.arpa`` string) when reverse lookup fails, which
    would land in the published TXT record. Pin the implementation
    so a future refactor doesn't reintroduce the call.
    """
    monkeypatch.setattr(socket, "gethostname", lambda: "host")

    def _boom() -> str:
        msg = "getfqdn must not be called in this code path"
        raise AssertionError(msg)

    monkeypatch.setattr(socket, "getfqdn", _boom)
    assert _default_hostname() == "host.local"


# ---------------------------------------------------------------------------
# build_service_info
# ---------------------------------------------------------------------------


def test_build_service_info_populates_txt_and_server() -> None:
    advertiser = _make_advertiser(name="green", hostname="green.local")
    info = advertiser.build_service_info()
    assert info.type == SERVICE_TYPE
    assert info.name == f"green.{SERVICE_TYPE}"
    assert info.port == 6052
    # ServiceInfo encodes properties as bytes; decode to compare.
    decoded = {k.decode(): v.decode() for k, v in info.properties.items()}
    assert decoded == {
        "server_version": "1.2.3",
        "esphome_version": "2026.5.0",
        "name": "green",
        "hostname": "green.local",
    }
    # ``server`` is always trailing-dotted so zeroconf doesn't double-suffix it.
    assert info.server == "green.local."


def test_build_service_info_keeps_trailing_dot_on_explicit_fqdn() -> None:
    """An already-trailing-dot hostname round-trips unchanged."""
    advertiser = _make_advertiser(name="green", hostname="green.local.")
    info = advertiser.build_service_info()
    assert info.server == "green.local."


# ---------------------------------------------------------------------------
# register / unregister lifecycle
# ---------------------------------------------------------------------------


def _make_zeroconf_mock() -> MagicMock:
    zc = MagicMock()
    zc.async_register_service = AsyncMock()
    zc.async_unregister_service = AsyncMock()
    return zc


@pytest.mark.asyncio
async def test_register_calls_async_register_service() -> None:
    advertiser = _make_advertiser(name="green", hostname="green.local")
    zc = _make_zeroconf_mock()
    await advertiser.register(zc)
    assert advertiser.registered is True
    zc.async_register_service.assert_awaited_once()
    args, kwargs = zc.async_register_service.call_args
    info = args[0]
    assert info.type == SERVICE_TYPE
    assert info.port == 6052
    assert kwargs.get("allow_name_change") is True


@pytest.mark.asyncio
async def test_register_is_idempotent() -> None:
    advertiser = _make_advertiser(name="green", hostname="green.local")
    zc = _make_zeroconf_mock()
    await advertiser.register(zc)
    await advertiser.register(zc)
    # Second register is a no-op — exactly one call regardless.
    zc.async_register_service.assert_awaited_once()


@pytest.mark.asyncio
async def test_register_failure_clears_state() -> None:
    """A zeroconf register-side error leaves the advertiser unregistered.

    Subsequent ``unregister`` calls should be no-ops (no spurious
    ``async_unregister_service`` against a never-registered info)
    and the dashboard's shutdown path stays clean.
    """
    advertiser = _make_advertiser(name="green", hostname="green.local")
    zc = _make_zeroconf_mock()
    zc.async_register_service.side_effect = RuntimeError("zeroconf is sad")
    await advertiser.register(zc)
    assert advertiser.registered is False
    await advertiser.unregister()
    zc.async_unregister_service.assert_not_awaited()


@pytest.mark.asyncio
async def test_unregister_calls_async_unregister_service() -> None:
    advertiser = _make_advertiser(name="green", hostname="green.local")
    zc = _make_zeroconf_mock()
    await advertiser.register(zc)
    await advertiser.unregister()
    assert advertiser.registered is False
    zc.async_unregister_service.assert_awaited_once()


@pytest.mark.asyncio
async def test_unregister_without_register_is_noop() -> None:
    advertiser = _make_advertiser(name="green", hostname="green.local")
    await advertiser.unregister()
    assert advertiser.registered is False


@pytest.mark.asyncio
async def test_unregister_swallows_zeroconf_errors() -> None:
    """A teardown-time zeroconf failure must not surface to the caller."""
    advertiser = _make_advertiser(name="green", hostname="green.local")
    zc = _make_zeroconf_mock()
    zc.async_unregister_service.side_effect = RuntimeError("socket already closed")
    await advertiser.register(zc)
    await advertiser.unregister()  # must not raise
    assert advertiser.registered is False


# ---------------------------------------------------------------------------
# DeviceBuilder integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_device_builder_skips_advertise_when_zeroconf_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    make_settings,
    _hermetic_lifecycle,
) -> None:
    """Zeroconf failed to bind → advertise is skipped, no construction.

    The hermetic-lifecycle fixture stubs ``DeviceStateMonitor.start``
    to a no-op, so ``_zeroconf`` stays ``None`` (matches the
    production "port 5353 was held" failure mode). The advertise
    branch must short-circuit cleanly without surfacing an error.
    """
    constructed: list[object] = []

    class _FakeAdvertiser:
        def __init__(self, **kwargs: object) -> None:
            constructed.append(kwargs)
            self.register = AsyncMock()
            self.unregister = AsyncMock()

    monkeypatch.setattr(db_module, "DashboardAdvertiser", _FakeAdvertiser)

    db = DeviceBuilder(make_settings(with_core_path=True))
    try:
        await db.start()
    finally:
        await db.stop()

    assert constructed == [], "advertise must skip when zeroconf is None"


@pytest.mark.asyncio
async def test_device_builder_skips_advertise_in_ha_addon_mode(
    monkeypatch: pytest.MonkeyPatch,
    make_settings,
    _hermetic_lifecycle,
) -> None:
    """``on_ha_addon=True`` → advertise is skipped even with zeroconf up.

    Mocks the state monitor's ``zeroconf`` accessor to return a live
    object so the only thing standing between ``start()`` and a
    ``DashboardAdvertiser`` construction is the addon-mode guard.
    """
    constructed: list[object] = []

    class _FakeAdvertiser:
        def __init__(self, **kwargs: object) -> None:
            constructed.append(kwargs)
            self.register = AsyncMock()
            self.unregister = AsyncMock()

    monkeypatch.setattr(db_module, "DashboardAdvertiser", _FakeAdvertiser)
    monkeypatch.setattr(DeviceStateMonitor, "zeroconf", property(lambda self: MagicMock()))

    settings = make_settings(with_core_path=True)
    settings.on_ha_addon = True
    db = DeviceBuilder(settings)
    try:
        await db.start()
    finally:
        await db.stop()

    assert constructed == [], "advertise must be skipped in HA addon mode"


@pytest.mark.asyncio
async def test_device_builder_constructs_advertiser_when_zeroconf_present(
    monkeypatch: pytest.MonkeyPatch,
    make_settings,
    _hermetic_lifecycle,
) -> None:
    """Non-addon mode + zeroconf up → advertise is registered and unregistered."""
    fake_zc = MagicMock()
    instances: list[object] = []

    class _FakeAdvertiser:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.register = AsyncMock()
            self.unregister = AsyncMock()
            instances.append(self)

    monkeypatch.setattr(db_module, "DashboardAdvertiser", _FakeAdvertiser)
    monkeypatch.setattr(DeviceStateMonitor, "zeroconf", property(lambda self: fake_zc))

    settings = make_settings(with_core_path=True)
    settings.on_ha_addon = False
    settings.port = 6052
    db = DeviceBuilder(settings)
    try:
        await db.start()
        assert len(instances) == 1
        adv = instances[0]
        adv.register.assert_awaited_once_with(fake_zc)  # type: ignore[attr-defined]
        # Constructor sees the configured port + the right version fields.
        assert adv.kwargs["port"] == 6052  # type: ignore[attr-defined]
        assert "server_version" in adv.kwargs  # type: ignore[attr-defined]
        assert "esphome_version" in adv.kwargs  # type: ignore[attr-defined]
    finally:
        await db.stop()
        adv.unregister.assert_awaited_once()  # type: ignore[attr-defined]
