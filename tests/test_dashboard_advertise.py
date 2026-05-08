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

import asyncio
import socket
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder import device_builder as db_module
from esphome_device_builder.controllers._device_state_monitor import DeviceStateMonitor
from esphome_device_builder.device_builder import DeviceBuilder
from esphome_device_builder.helpers import dashboard_advertise
from esphome_device_builder.helpers.dashboard_advertise import (
    SERVICE_TYPE,
    DashboardAdvertiser,
    _default_friendly_name,
    _default_hostname,
    _local_addresses,
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


def test_default_hostname_returns_empty_when_gethostname_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blank ``gethostname`` (rare but seen on minimal containers) yields ``""``."""
    monkeypatch.setattr(socket, "gethostname", lambda: "")
    assert _default_hostname() == ""


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
# _local_addresses — adapter enumeration / filtering
# ---------------------------------------------------------------------------


def _adapter(name: str, *, nice_name: str | None = None, ips: list[object]) -> object:
    """Build a stand-in for an ``ifaddr.Adapter`` with the fields we read."""
    ip_objs = [
        type(
            "IP",
            (),
            {
                "ip": raw,
                "network_prefix": 0,
                "nice_name": "",
                "is_IPv4": isinstance(raw, str),
                "is_IPv6": isinstance(raw, tuple),
            },
        )()
        for raw in ips
    ]
    return type(
        "Adapter",
        (),
        {
            "name": name,
            "nice_name": nice_name or name,
            "ips": ip_objs,
            "index": 0,
        },
    )()


def test_local_addresses_filters_loopback_interface(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole loopback interface is dropped, including its link-locals."""
    adapters = [
        _adapter(
            "lo0",
            ips=["127.0.0.1", ("::1", 0, 0), ("fe80::1", 0, 1)],
        ),
        _adapter("en0", ips=["192.168.1.10"]),
    ]
    monkeypatch.setattr(dashboard_advertise.ifaddr, "get_adapters", lambda: adapters)
    assert _local_addresses() == ["192.168.1.10"]


def test_local_addresses_filters_loopback_by_nice_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-``lo*`` adapter name still gets dropped if Windows-style 'Loopback'."""
    adapters = [
        _adapter(
            "\\Device\\NPF_Loopback", nice_name="Loopback Pseudo-Interface", ips=["127.0.0.1"]
        ),
        _adapter("Ethernet", ips=["10.0.0.5"]),
    ]
    monkeypatch.setattr(dashboard_advertise.ifaddr, "get_adapters", lambda: adapters)
    assert _local_addresses() == ["10.0.0.5"]


def test_local_addresses_drops_link_local_ipv6(monkeypatch: pytest.MonkeyPatch) -> None:
    """IPv6 link-local (``fe80::/10``) is dropped — wire can't carry scope_id."""
    adapters = [
        _adapter(
            "en0",
            ips=[
                "192.168.1.10",
                ("fe80::1234:5678:abcd:ef00", 0, 4),
                ("2001:db8::1", 0, 0),
                ("fdc8:d776:7cca:46ed::1", 0, 0),  # ULA — kept
            ],
        ),
    ]
    monkeypatch.setattr(dashboard_advertise.ifaddr, "get_adapters", lambda: adapters)
    result = _local_addresses()
    assert "192.168.1.10" in result
    assert "2001:db8::1" in result
    assert "fdc8:d776:7cca:46ed::1" in result
    assert all("fe80" not in addr for addr in result)


def test_local_addresses_drops_loopback_ip_on_real_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defense in depth: a 127.x address aliased onto a real interface is dropped."""
    adapters = [
        _adapter("en0", ips=["192.168.1.10", "127.0.0.1"]),
    ]
    monkeypatch.setattr(dashboard_advertise.ifaddr, "get_adapters", lambda: adapters)
    assert _local_addresses() == ["192.168.1.10"]


def test_local_addresses_skips_unparseable_strings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Garbage from a flaky adapter doesn't blow up the whole walk."""
    adapters = [
        _adapter("en0", ips=["192.168.1.10", "not-an-ip"]),
    ]
    monkeypatch.setattr(dashboard_advertise.ifaddr, "get_adapters", lambda: adapters)
    assert _local_addresses() == ["192.168.1.10"]


def test_local_addresses_returns_empty_when_no_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    """No adapters at all — return an empty list, not a crash."""
    monkeypatch.setattr(dashboard_advertise.ifaddr, "get_adapters", lambda: [])
    assert _local_addresses() == []


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
    # TXT carries only the version fields that aren't already
    # implied by the browse response. Friendly name and hostname
    # come from ``info.name`` and ``info.server`` instead — pinned
    # below so a future refactor doesn't quietly add them back.
    assert decoded == {
        "server_version": "1.2.3",
        "esphome_version": "2026.5.0",
    }
    # ``server`` is always trailing-dotted so zeroconf doesn't double-suffix it.
    assert info.server == "green.local."


def test_build_service_info_keeps_trailing_dot_on_explicit_fqdn() -> None:
    """An already-trailing-dot hostname round-trips unchanged."""
    advertiser = _make_advertiser(name="green", hostname="green.local.")
    info = advertiser.build_service_info(addresses=[])
    assert info.server == "green.local."


def test_service_type_property_is_canonical() -> None:
    """The ``service_type`` accessor returns the module-level constant."""
    advertiser = _make_advertiser(name="green", hostname="green.local")
    assert advertiser.service_type == SERVICE_TYPE


# ---------------------------------------------------------------------------
# register / unregister lifecycle
# ---------------------------------------------------------------------------


def _make_zeroconf_mock() -> MagicMock:
    zc = MagicMock()
    zc.async_register_service = AsyncMock()
    zc.async_unregister_service = AsyncMock()
    return zc


@pytest.mark.asyncio
async def test_register_calls_async_register_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dashboard_advertise, "_local_addresses", lambda: ["192.168.1.10"])
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
    # Pin the executor-fetched addresses landed on the published info.
    assert info.parsed_addresses() == ["192.168.1.10"]


@pytest.mark.asyncio
async def test_register_runs_address_enumeration_in_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``_local_addresses`` must run on a thread, not the event loop.

    ``ifaddr.get_adapters`` does blocking I/O (``/proc/net`` on Linux,
    Win32 calls on Windows). Calling it directly on the loop would
    stall every concurrent request and trip blockbuster on Linux CI.
    Verify by spying on the loop's ``run_in_executor`` to confirm
    the helper is dispatched there.
    """
    advertiser = _make_advertiser(name="green", hostname="green.local")
    zc = _make_zeroconf_mock()

    loop = asyncio.get_running_loop()
    real_run_in_executor = loop.run_in_executor
    captured: list[object] = []

    def _spy(executor: object, func: object, *args: object) -> object:
        captured.append(func)
        return real_run_in_executor(executor, func, *args)

    monkeypatch.setattr(loop, "run_in_executor", _spy)
    await advertiser.register(zc)
    # ``_local_addresses`` is the function we expect on the executor.
    assert dashboard_advertise._local_addresses in captured


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
