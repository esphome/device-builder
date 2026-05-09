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
import contextlib
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
    pin_sha256: str | None = None,
) -> DashboardAdvertiser:
    return DashboardAdvertiser(
        port=port,
        server_version="1.2.3",
        esphome_version="2026.5.0",
        pin_sha256=pin_sha256,
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


def test_local_addresses_drops_link_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Both IPv4 and IPv6 link-local addresses are dropped.

    IPv6 ``fe80::/10`` is unreachable once the scope_id is stripped
    by the wire format. IPv4 ``169.254.0.0/16`` (APIPA) only shows
    up when DHCP has failed — advertising it just attracts pairings
    that immediately break the next time DHCP comes back.
    """
    adapters = [
        _adapter(
            "en0",
            ips=[
                "192.168.1.10",
                "169.254.7.7",  # IPv4 APIPA — dropped
                ("fe80::1234:5678:abcd:ef00", 0, 4),  # IPv6 link-local — dropped
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
    assert "169.254.7.7" not in result
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


def test_local_addresses_deduplicates_repeated_ips(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    The same IP appearing on multiple adapters lands in the result once.

    A duplicate would inflate the published A/AAAA list and worse,
    if the duplicate flickered between two enumerations (one
    interface up, the other down) the sorted set comparison in
    ``refresh`` would see "different" and fire a spurious update.
    Pin the dedup so refresh stays a true no-op when nothing
    actually changed.
    """
    adapters = [
        _adapter("en0", ips=["192.168.1.10"]),
        _adapter("en1", ips=["192.168.1.10", "10.0.0.5"]),
    ]
    monkeypatch.setattr(dashboard_advertise.ifaddr, "get_adapters", lambda: adapters)
    result = _local_addresses()
    assert result == ["192.168.1.10", "10.0.0.5"]
    assert len(result) == len(set(result))


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


def test_build_service_info_carries_pin_sha256_when_set() -> None:
    """``pin_sha256`` lands in TXT when the advertiser was constructed with it."""
    pin = "a" * 64
    advertiser = _make_advertiser(name="green", hostname="green.local", pin_sha256=pin)
    info = advertiser.build_service_info()
    decoded = {k.decode(): v.decode() for k, v in info.properties.items()}
    assert decoded["pin_sha256"] == pin


def test_build_service_info_omits_pin_sha256_when_unset() -> None:
    """``pin_sha256`` is absent from TXT when the advertiser doesn't have one."""
    advertiser = _make_advertiser(name="green", hostname="green.local")
    info = advertiser.build_service_info()
    decoded = {k.decode(): v.decode() for k, v in info.properties.items()}
    assert "pin_sha256" not in decoded


def test_set_pin_sha256_updates_subsequent_advertise() -> None:
    """``set_pin_sha256`` makes the next ``build_service_info`` carry the new pin."""
    advertiser = _make_advertiser(name="green", hostname="green.local")
    advertiser.set_pin_sha256("b" * 64)
    info = advertiser.build_service_info()
    decoded = {k.decode(): v.decode() for k, v in info.properties.items()}
    assert decoded["pin_sha256"] == "b" * 64


def test_build_service_info_carries_remote_build_port_when_set() -> None:
    """``remote_build_port`` lands in TXT as a stringified int when set."""
    advertiser = DashboardAdvertiser(
        port=6052,
        server_version="1.2.3",
        esphome_version="2026.5.0",
        remote_build_port=6055,
    )
    info = advertiser.build_service_info()
    decoded = {k.decode(): v.decode() for k, v in info.properties.items()}
    assert decoded["remote_build_port"] == "6055"


def test_build_service_info_omits_remote_build_port_when_unset() -> None:
    """``remote_build_port`` is absent when the listener isn't bound."""
    advertiser = _make_advertiser()
    info = advertiser.build_service_info()
    decoded = {k.decode(): v.decode() for k, v in info.properties.items()}
    assert "remote_build_port" not in decoded


def test_set_remote_build_port_updates_subsequent_advertise() -> None:
    """``set_remote_build_port`` makes the next advertise carry the new port."""
    advertiser = _make_advertiser()
    advertiser.set_remote_build_port(7000)
    info = advertiser.build_service_info()
    decoded = {k.decode(): v.decode() for k, v in info.properties.items()}
    assert decoded["remote_build_port"] == "7000"


@pytest.mark.asyncio
async def test_refresh_republishes_when_only_txt_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A TXT-only change triggers ``async_update_service`` on the next refresh.

    Pre-fix, ``refresh`` short-circuited when the address set was
    unchanged, so a setter-driven TXT update (``set_pin_sha256``,
    ``set_remote_build_port``) never made it onto the wire after
    the initial register. The fix detects TXT differences too;
    pin that contract.
    """
    advertiser = _make_advertiser()
    # Pre-seed an "already registered" state. We don't actually
    # talk to a real zeroconf instance — fake the bits ``refresh``
    # reads / writes.
    initial_addresses = ["10.0.0.5"]
    monkeypatch.setattr(
        "esphome_device_builder.helpers.dashboard_advertise._local_addresses",
        lambda: list(initial_addresses),
    )
    advertiser._info = advertiser.build_service_info(initial_addresses)
    fake_zeroconf = MagicMock()
    fake_zeroconf.async_update_service = AsyncMock()
    advertiser._zeroconf = fake_zeroconf

    # Now set a TXT field WITHOUT changing addresses.
    advertiser.set_pin_sha256("a" * 64)
    refreshed = await advertiser.refresh()

    assert refreshed is True
    assert fake_zeroconf.async_update_service.called
    # The republished info carries the new pin.
    new_info = fake_zeroconf.async_update_service.call_args.args[0]
    decoded = {k.decode(): v.decode() for k, v in new_info.properties.items()}
    assert decoded["pin_sha256"] == "a" * 64


@pytest.mark.asyncio
async def test_refresh_no_op_when_nothing_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``refresh`` returns False without calling zeroconf when nothing changed."""
    advertiser = _make_advertiser()
    initial_addresses = ["10.0.0.5"]
    monkeypatch.setattr(
        "esphome_device_builder.helpers.dashboard_advertise._local_addresses",
        lambda: list(initial_addresses),
    )
    advertiser._info = advertiser.build_service_info(initial_addresses)
    fake_zeroconf = MagicMock()
    fake_zeroconf.async_update_service = AsyncMock()
    advertiser._zeroconf = fake_zeroconf

    refreshed = await advertiser.refresh()

    assert refreshed is False
    assert not fake_zeroconf.async_update_service.called


def test_build_service_info_keeps_trailing_dot_on_explicit_fqdn() -> None:
    """An already-trailing-dot hostname round-trips unchanged."""
    advertiser = _make_advertiser(name="green", hostname="green.local.")
    info = advertiser.build_service_info(addresses=[])
    assert info.server == "green.local."


def test_service_type_property_is_canonical() -> None:
    """The ``service_type`` accessor returns the module-level constant."""
    advertiser = _make_advertiser(name="green", hostname="green.local")
    assert advertiser.service_type == SERVICE_TYPE


def test_service_instance_name_returns_none_before_register() -> None:
    """``service_instance_name`` is ``None`` until ``register()`` succeeds."""
    advertiser = _make_advertiser(name="green", hostname="green.local")
    assert advertiser.service_instance_name is None


@pytest.mark.asyncio
async def test_service_instance_name_returns_published_name_after_register(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Post-``register``, the accessor returns what zeroconf published.

    Public surface so peer-discovery code (the
    ``RemoteBuildController`` browser) can filter our own
    broadcast out of its discovered list without reaching into
    the private ``_info`` attribute.
    """
    monkeypatch.setattr(dashboard_advertise, "_local_addresses", lambda: ["192.168.1.10"])
    advertiser = _make_advertiser(name="green", hostname="green.local")
    zc = _make_zeroconf_mock()
    await advertiser.register(zc)
    try:
        assert advertiser.service_instance_name == f"green.{SERVICE_TYPE}"
    finally:
        await advertiser.unregister()
    assert advertiser.service_instance_name is None


def test_build_service_info_falls_back_when_hostname_is_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Blank hostname → SRV target derived from the friendly name.

    When ``socket.gethostname`` is blank (minimal containers,
    misconfigured systems), ``_default_hostname`` returns ``""``.
    Without a fallback, ``build_service_info`` would set
    ``server="."`` — an invalid SRV target that python-zeroconf
    rejects at register time. Pin the recovery so the advertise
    still produces a valid record on degraded hosts.
    """
    monkeypatch.setattr(socket, "gethostname", lambda: "")
    # Both inherit the ``""`` from gethostname; ``_default_friendly_name``
    # rescues with ``"esphome-dashboard"`` and we want SRV target to
    # follow.
    advertiser = DashboardAdvertiser(port=6052, server_version="1.0", esphome_version="2026.5.0")
    info = advertiser.build_service_info(addresses=[])
    assert info.server == "esphome-dashboard.local."


# ---------------------------------------------------------------------------
# register / unregister lifecycle
# ---------------------------------------------------------------------------


def _make_zeroconf_mock() -> MagicMock:
    zc = MagicMock()
    zc.async_register_service = AsyncMock()
    zc.async_unregister_service = AsyncMock()
    zc.async_update_service = AsyncMock()
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
async def test_register_starts_refresh_loop_task() -> None:
    """``register`` spawns a named background task that drives the refresh tick."""
    advertiser = _make_advertiser(name="green", hostname="green.local")
    zc = _make_zeroconf_mock()
    await advertiser.register(zc)
    task = advertiser._refresh_task
    try:
        assert task is not None
        assert not task.done()
        assert task.get_name() == "dashboard-advertise-refresh"
    finally:
        await advertiser.unregister()


@pytest.mark.asyncio
async def test_refresh_loop_invokes_refresh_on_each_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Refresh is invoked once per ``_REFRESH_INTERVAL_SECONDS`` tick.

    The 5-minute sleep is patched to a tiny value so the test can
    observe two ticks without sitting around for ten minutes.
    """
    monkeypatch.setattr(dashboard_advertise, "_REFRESH_INTERVAL_SECONDS", 0.01)
    refresh_count = 0

    async def _counted_refresh(self: object) -> bool:
        nonlocal refresh_count
        refresh_count += 1
        return False

    monkeypatch.setattr(DashboardAdvertiser, "refresh", _counted_refresh)
    advertiser = _make_advertiser(name="green", hostname="green.local")
    zc = _make_zeroconf_mock()
    await advertiser.register(zc)
    try:
        # Two ticks of 0.01s each — yield until refresh has been
        # called at least twice or 1s elapses (whichever first).
        for _ in range(100):
            if refresh_count >= 2:
                break
            await asyncio.sleep(0.02)
        assert refresh_count >= 2
    finally:
        await advertiser.unregister()


@pytest.mark.asyncio
async def test_refresh_loop_survives_refresh_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient refresh failure must not kill the periodic loop."""
    monkeypatch.setattr(dashboard_advertise, "_REFRESH_INTERVAL_SECONDS", 0.01)
    calls = 0

    async def _raising_refresh(self: object) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            msg = "transient zeroconf glitch"
            raise RuntimeError(msg)
        return False

    monkeypatch.setattr(DashboardAdvertiser, "refresh", _raising_refresh)
    advertiser = _make_advertiser(name="green", hostname="green.local")
    zc = _make_zeroconf_mock()
    await advertiser.register(zc)
    try:
        for _ in range(100):
            if calls >= 2:
                break
            await asyncio.sleep(0.02)
        # First tick raised, second tick still ran — loop is alive.
        assert calls >= 2
        assert advertiser._refresh_task is not None
        assert not advertiser._refresh_task.done()
    finally:
        await advertiser.unregister()


@pytest.mark.asyncio
async def test_unregister_cancels_refresh_loop() -> None:
    """``unregister`` drains the periodic-refresh task before tearing down."""
    advertiser = _make_advertiser(name="green", hostname="green.local")
    zc = _make_zeroconf_mock()
    await advertiser.register(zc)
    task = advertiser._refresh_task
    assert task is not None
    await advertiser.unregister()
    assert task.done()
    assert advertiser._refresh_task is None


@pytest.mark.asyncio
async def test_unregister_swallows_refresh_task_exception() -> None:
    """
    Drain a refresh task that ended in a non-``CancelledError`` exception.

    A refresh-task that ended with a non-``CancelledError`` exception
    is drained quietly so dashboard shutdown stays clean.

    The production refresh loop catches its own exceptions, so this
    branch only fires if something replaces the task with one that
    doesn't — defense in depth, pinned with an explicit test.
    """
    advertiser = _make_advertiser(name="green", hostname="green.local")
    zc = _make_zeroconf_mock()
    await advertiser.register(zc)

    async def _failing() -> None:
        msg = "task blew up"
        raise RuntimeError(msg)

    # Replace the running refresh task with one that ends in a
    # non-CancelledError exception. Cancel the original first so it
    # doesn't leak.
    if advertiser._refresh_task is not None:
        advertiser._refresh_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await advertiser._refresh_task
    advertiser._refresh_task = asyncio.create_task(_failing())
    # Give the failing task a tick to actually finish.
    await asyncio.sleep(0)

    # Must not raise.
    await advertiser.unregister()
    assert advertiser._refresh_task is None


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
async def test_refresh_skips_when_addresses_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Calling ``refresh`` when ``_local_addresses`` hasn't changed is a no-op.

    Avoids spamming the network with an announcement when the
    interface set is stable. The contract is "only refresh if
    something actually changed".
    """
    monkeypatch.setattr(dashboard_advertise, "_local_addresses", lambda: ["192.168.1.10"])
    advertiser = _make_advertiser(name="green", hostname="green.local")
    zc = _make_zeroconf_mock()
    await advertiser.register(zc)
    zc.async_update_service.reset_mock()

    changed = await advertiser.refresh()

    assert changed is False
    zc.async_update_service.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_publishes_via_update_service_when_addresses_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Address-set change → ``async_update_service`` fires with the new info.

    Uses the proper update API (not unregister + re-register) so peers
    that have the existing record cached see a single record-change
    rather than a removal followed by a re-add.
    """
    addresses = ["192.168.1.10"]
    monkeypatch.setattr(dashboard_advertise, "_local_addresses", lambda: list(addresses))
    advertiser = _make_advertiser(name="green", hostname="green.local")
    zc = _make_zeroconf_mock()
    await advertiser.register(zc)
    zc.async_update_service.reset_mock()
    zc.async_register_service.reset_mock()

    # DHCP renewal flips the address.
    addresses[:] = ["192.168.1.42", "fdc8::1"]
    changed = await advertiser.refresh()

    assert changed is True
    zc.async_update_service.assert_awaited_once()
    zc.async_register_service.assert_not_awaited()
    new_info = zc.async_update_service.call_args.args[0]
    assert sorted(new_info.parsed_addresses()) == sorted(["192.168.1.42", "fdc8::1"])


@pytest.mark.asyncio
async def test_refresh_is_noop_when_not_registered() -> None:
    """``refresh()`` before ``register()`` is a no-op (no zeroconf to talk to)."""
    advertiser = _make_advertiser(name="green", hostname="green.local")
    assert await advertiser.refresh() is False


@pytest.mark.asyncio
async def test_refresh_swallows_update_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """A zeroconf update-side error doesn't surface to the caller."""
    addresses = ["192.168.1.10"]
    monkeypatch.setattr(dashboard_advertise, "_local_addresses", lambda: list(addresses))
    advertiser = _make_advertiser(name="green", hostname="green.local")
    zc = _make_zeroconf_mock()
    await advertiser.register(zc)
    zc.async_update_service.side_effect = RuntimeError("update failed")

    addresses[:] = ["192.168.1.42"]
    changed = await advertiser.refresh()

    # Update fired, but the wrapper caught the error and returned False;
    # the cached info stays at the pre-update value so a future retry
    # still sees the change.
    assert changed is False
    zc.async_update_service.assert_awaited_once()


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
            self.registered = False
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
            self.registered = False
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
            self.registered = False
            self.unregister = AsyncMock()
            instances.append(self)

    monkeypatch.setattr(db_module, "DashboardAdvertiser", _FakeAdvertiser)
    monkeypatch.setattr(DeviceStateMonitor, "zeroconf", property(lambda self: fake_zc))

    settings = make_settings(with_core_path=True)
    settings.on_ha_addon = False
    settings.port = 6052
    db = DeviceBuilder(settings)
    # Seed ``adv`` to ``None`` so the ``finally`` block can guard
    # against a failure in ``db.start()`` that would otherwise leave
    # ``adv`` unbound and mask the real assertion error with an
    # ``UnboundLocalError`` at teardown.
    adv: object | None = None
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
        if adv is not None:
            adv.unregister.assert_awaited_once()  # type: ignore[attr-defined]
