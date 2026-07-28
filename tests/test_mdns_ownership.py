"""Tests for the mDNS ownership lifecycle: claims behind a live PTR, withdrawals to ping."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from zeroconf.const import _TYPE_A, _TYPE_AAAA, _TYPE_SRV, _TYPE_TXT

from esphome_device_builder.controllers._device_state_monitor import mdns as mdns_module
from esphome_device_builder.controllers._device_state_monitor import shared
from esphome_device_builder.models import DeviceState, ReachabilitySource

from .conftest import (
    make_online_api_device,
    make_state_monitor_with_callbacks,
    stub_async_service_info,
)

_SERVICE_NAME = "kitchen._esphomelib._tcp.local."


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({}, id="api"),
        pytest.param({"api_enabled": False, "loaded_integrations": ["mqtt", "wifi"]}, id="non_api"),
    ],
)
def test_should_ping_skips_mdns_owned_devices(overrides: dict[str, Any]) -> None:
    """Mdns ownership rides the browser lifecycle — owned ONLINE devices leave the sweep."""
    device = make_online_api_device(**overrides)
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.MDNS

    assert shared.should_ping(monitor, device) is False


def test_has_cached_trace_checks_each_record_bucket() -> None:
    """Address, SRV, TXT, and live-PTR buckets each independently count as a trace."""
    monitor, _callbacks = make_state_monitor_with_callbacks([make_online_api_device()])
    fake_zeroconf = MagicMock()
    monitor.mdns._zeroconf = fake_zeroconf
    cache = fake_zeroconf.zeroconf.cache
    buckets: dict[tuple[str, int], list[Any]] = {}
    cache.get_all_by_details.side_effect = lambda name, type_, _cls: buckets.get((name, type_), [])
    cache.current_entry_with_name_and_alias.return_value = None
    assert monitor.mdns.has_cached_trace("kitchen") is False

    for bucket_key in [
        ("kitchen.local.", _TYPE_A),
        ("kitchen.local.", _TYPE_AAAA),
        (_SERVICE_NAME, _TYPE_SRV),
        (_SERVICE_NAME, _TYPE_TXT),
    ]:
        buckets[bucket_key] = [MagicMock()]
        assert monitor.mdns.has_cached_trace("kitchen") is True, bucket_key
        buckets.clear()

    cache.current_entry_with_name_and_alias.return_value = MagicMock()
    assert monitor.mdns.has_cached_trace("kitchen") is True

    monitor.mdns._zeroconf = None
    assert monitor.mdns.has_cached_trace("kitchen") is False


def _dispatch_removed(monitor: Any) -> None:
    monitor.mdns._on_esphomelib_service_state_change(
        MagicMock(),
        "_esphomelib._tcp.local.",
        _SERVICE_NAME,
        mdns_module.ServiceStateChange.Removed,
    )


def _prime_removed(monitor: Any) -> None:
    """Give the Removed path a live ICMP arbiter and a stubbed sweep wake."""
    monitor.ping.icmp_available = True
    monitor.ping.wake = MagicMock()


async def test_removed_marks_unknown_and_wakes_the_ping_sweep() -> None:
    """A ``Removed`` drops to UNKNOWN, releases every ledger, and nudges the ICMP sweep."""
    device = make_online_api_device()
    monitor, callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.MDNS
    _prime_removed(monitor)

    _dispatch_removed(monitor)

    assert device.runtime_state.state == DeviceState.UNKNOWN
    assert "kitchen" not in monitor.state.state_source
    assert device.runtime_state.ip_addresses == []
    assert device.ip == "192.168.1.50"
    monitor.ping.wake.assert_called_once()
    assert callbacks.calls_for("on_source_change") == [
        ("on_source_change", "kitchen", ReachabilitySource.UNKNOWN),
    ]


async def test_removed_then_ping_miss_goes_offline() -> None:
    """Goodbye then ICMP silence lands OFFLINE via ping (#2369)."""
    device = make_online_api_device()
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.MDNS
    _prime_removed(monitor)

    _dispatch_removed(monitor)
    shared.apply_ping_result(monitor, "kitchen", None)

    assert device.runtime_state.state == DeviceState.OFFLINE
    assert monitor.state.state_source["kitchen"] == ReachabilitySource.PING


async def test_removed_then_ping_answer_comes_back_online_via_ping() -> None:
    """A live device demoted by a spurious ``Removed`` revives under the ping source."""
    device = make_online_api_device()
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.MDNS
    _prime_removed(monitor)

    _dispatch_removed(monitor)
    shared.apply_ping_result(monitor, "kitchen", 2.5)

    assert device.runtime_state.state == DeviceState.ONLINE
    assert monitor.state.state_source["kitchen"] == ReachabilitySource.PING


async def test_added_after_removed_reclaims_mdns_and_outranks_a_late_ping_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-announce re-claims mdns ownership; a stale ping miss can't demote it."""
    device = make_online_api_device()
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.MDNS
    _prime_removed(monitor)
    stub_async_service_info(monkeypatch, cached=True)
    # The re-announce lands a live esphomelib PTR (no http sibling),
    # opening the cache-claim gate without deferring the withdrawal.
    zc = MagicMock()
    zc.zeroconf.cache.current_entry_with_name_and_alias.side_effect = lambda type_, _alias: (
        MagicMock() if type_ == "_esphomelib._tcp.local." else None
    )
    monitor.mdns._zeroconf = zc

    _dispatch_removed(monitor)
    assert device.runtime_state.state == DeviceState.UNKNOWN

    monitor.mdns._on_esphomelib_service_state_change(
        MagicMock(), "_esphomelib._tcp.local.", _SERVICE_NAME, mdns_module.ServiceStateChange.Added
    )
    assert device.runtime_state.state == DeviceState.ONLINE
    assert monitor.state.state_source["kitchen"] == ReachabilitySource.MDNS

    shared.apply_ping_result(monitor, "kitchen", None)
    assert device.runtime_state.state == DeviceState.ONLINE
    assert monitor.state.state_source["kitchen"] == ReachabilitySource.MDNS


async def test_removed_without_icmp_goes_offline_directly() -> None:
    """With no ICMP arbiter the withdrawal itself demotes instead of parking on UNKNOWN."""
    device = make_online_api_device()
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.MDNS
    monitor.ping.wake = MagicMock()  # type: ignore[method-assign]
    monitor.ping.icmp_available = False

    _dispatch_removed(monitor)

    assert device.runtime_state.state == DeviceState.OFFLINE
    assert "kitchen" not in monitor.state.state_source
    assert device.ip == "192.168.1.50"


async def test_removed_before_the_icmp_probe_demotes_offline() -> None:
    """An undecided probe demotes too — the probe may land False with no sweep ever running."""
    device = make_online_api_device()
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.MDNS
    monitor.ping.wake = MagicMock()  # type: ignore[method-assign]
    assert monitor.ping.icmp_available is None

    _dispatch_removed(monitor)

    assert device.runtime_state.state == DeviceState.OFFLINE
    assert "kitchen" not in monitor.state.state_source


async def test_removed_on_a_ping_settled_device_is_a_no_op() -> None:
    """A late PTR expiry can't un-confirm a ping-settled OFFLINE or touch its ledger."""
    device = make_online_api_device(state=DeviceState.OFFLINE)
    monitor, callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.PING
    _prime_removed(monitor)

    _dispatch_removed(monitor)

    assert device.runtime_state.state == DeviceState.OFFLINE
    assert callbacks.calls_for("on_state_change") == []
    assert monitor.state.state_source["kitchen"] == ReachabilitySource.PING


async def test_removed_leaves_a_ping_owned_online_device_alone() -> None:
    """MDNS withdraws only a claim it holds; a ping-owned device keeps its state."""
    device = make_online_api_device()
    monitor, callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.PING
    _prime_removed(monitor)

    _dispatch_removed(monitor)

    assert device.runtime_state.state == DeviceState.ONLINE
    assert monitor.state.state_source["kitchen"] == ReachabilitySource.PING
    assert callbacks.calls_for("on_source_change") == []


async def test_resolve_overlapping_a_withdrawal_does_not_reclaim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolve that started before the goodbye can't re-claim off its stale answer."""
    device = make_online_api_device()
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.MDNS
    _prime_removed(monitor)
    info = stub_async_service_info(monkeypatch)
    gate = asyncio.Event()

    async def _wire(*_args: Any, **_kwargs: Any) -> bool:
        await gate.wait()
        return True

    info.async_request = _wire
    resolve = asyncio.create_task(
        monitor.mdns.resolve_then(MagicMock(), info, "kitchen", monitor.mdns._apply_service_info)
    )
    await asyncio.sleep(0)

    _dispatch_removed(monitor)
    gate.set()
    assert await resolve is None

    assert device.runtime_state.state == DeviceState.UNKNOWN
    assert "kitchen" not in monitor.state.state_source


async def test_resolve_started_after_a_withdrawal_still_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-withdrawal resolve carries fresh evidence and re-claims behind a live PTR."""
    device = make_online_api_device()
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.MDNS
    _prime_removed(monitor)
    info = stub_async_service_info(monkeypatch, resolved=True)

    _dispatch_removed(monitor)
    assert device.runtime_state.state == DeviceState.UNKNOWN

    # The re-announce restored a live PTR before the resolve landed.
    zc = MagicMock()
    zc.zeroconf.cache.current_entry_with_name_and_alias.return_value = MagicMock()
    monitor.mdns._zeroconf = zc
    verdict = await monitor.mdns.resolve_then(
        MagicMock(), info, "kitchen", monitor.mdns._apply_service_info
    )

    assert verdict is True
    assert device.runtime_state.state == DeviceState.ONLINE
    assert monitor.state.state_source["kitchen"] == ReachabilitySource.MDNS


async def test_probe_after_withdrawal_does_not_reclaim_off_the_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lingering SRV/A satisfy the cache but can't vouch once the PTR is gone."""
    device = make_online_api_device()
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.MDNS
    _prime_removed(monitor)
    info = stub_async_service_info(monkeypatch, cached=True)
    zc = MagicMock()
    zc.zeroconf.cache.current_entry_with_name_and_alias.return_value = None
    monitor.mdns._zeroconf = zc

    _dispatch_removed(monitor)
    monitor.mdns.probe_device("kitchen")

    assert device.runtime_state.state == DeviceState.UNKNOWN
    assert "kitchen" not in monitor.state.state_source
    info.async_request.assert_not_called()


@pytest.mark.parametrize(
    "evidence",
    [
        pytest.param({"deployed_version": "2026.7.0"}, id="version"),
        pytest.param({"mac_address": "AA:BB:CC:DD:EE:FF"}, id="mac"),
    ],
)
async def test_withdrawal_hands_identity_back_for_known_api_device(
    evidence: dict[str, Any],
) -> None:
    """Releasing mdns ownership stamps ``deployed_identity_live`` when identity is known."""
    device = make_online_api_device(**evidence)
    monitor, callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.MDNS
    _prime_removed(monitor)

    _dispatch_removed(monitor)

    assert device.runtime_state.state == DeviceState.UNKNOWN
    assert ("on_deployed_identity_live_change", "kitchen", True) in callbacks.calls
    assert device.runtime_state.deployed_identity_live is True
    # Stamped before the source-change notification: no frame shows
    # both frontend-gate disjuncts down.
    events = [call[0] for call in callbacks.calls]
    assert events.index("on_deployed_identity_live_change") < events.index("on_source_change")


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({}, id="no_identity"),
        pytest.param(
            {"api_enabled": False, "loaded_integrations": ["wifi"], "deployed_version": "2026.7.0"},
            id="non_api",
        ),
    ],
)
async def test_withdrawal_without_identity_evidence_does_not_stamp(
    overrides: dict[str, Any],
) -> None:
    """No known api identity → the withdrawal stamps nothing."""
    device = make_online_api_device(**overrides)
    monitor, callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.MDNS
    _prime_removed(monitor)

    _dispatch_removed(monitor)

    assert callbacks.calls_for("on_deployed_identity_live_change") == []


async def test_readd_after_withdrawal_returns_vouching_to_the_announce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-announce takes mdns ownership back and blanks the hand-back stamp."""
    device = make_online_api_device(deployed_version="2026.7.0")
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.MDNS
    _prime_removed(monitor)
    stub_async_service_info(monkeypatch, cached=True)
    zc = MagicMock()
    zc.zeroconf.cache.current_entry_with_name_and_alias.side_effect = lambda type_, _alias: (
        MagicMock() if type_ == "_esphomelib._tcp.local." else None
    )
    monitor.mdns._zeroconf = zc

    _dispatch_removed(monitor)
    assert device.runtime_state.deployed_identity_live is True

    monitor.mdns._on_esphomelib_service_state_change(
        MagicMock(), "_esphomelib._tcp.local.", _SERVICE_NAME, mdns_module.ServiceStateChange.Added
    )

    assert device.runtime_state.state == DeviceState.ONLINE
    assert monitor.state.state_source["kitchen"] == ReachabilitySource.MDNS
    assert device.runtime_state.deployed_identity_live is False


async def test_wire_resolve_without_live_ptr_applies_data_but_takes_no_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PTR-less wire answer (a ``probe_device`` resolve) never claims mdns."""
    device = make_online_api_device(state=DeviceState.UNKNOWN)
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    info = stub_async_service_info(monkeypatch, resolved=True)
    zc = MagicMock()
    zc.zeroconf.cache.current_entry_with_name_and_alias.return_value = None
    monitor.mdns._zeroconf = zc

    verdict = await monitor.mdns.resolve_then(
        MagicMock(), info, "kitchen", monitor.mdns._apply_service_info
    )

    assert verdict is True
    assert device.runtime_state.state == DeviceState.UNKNOWN
    assert "kitchen" not in monitor.state.state_source
    assert device.runtime_state.deployed_version == "2026.7.0"


def _dispatch_http_removed(monitor: Any) -> None:
    monitor.mdns._on_http_service_state_change(
        MagicMock(),
        "_http._tcp.local.",
        "kitchen._http._tcp.local.",
        mdns_module.ServiceStateChange.Removed,
    )


async def test_http_removed_withdraws_a_non_api_bucket() -> None:
    """The ``_http._tcp`` PTR anchors non-API ownership; its Removed withdraws."""
    device = make_online_api_device(api_enabled=False, loaded_integrations=["web_server", "wifi"])
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.MDNS
    _prime_removed(monitor)

    _dispatch_http_removed(monitor)

    assert device.runtime_state.state == DeviceState.UNKNOWN
    assert "kitchen" not in monitor.state.state_source
    monitor.ping.wake.assert_called_once()


async def test_http_removed_defers_to_a_live_esphomelib_ptr() -> None:
    """The esphomelib lifecycle owns the name while its PTR is live."""
    device = make_online_api_device()
    monitor, callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.MDNS
    _prime_removed(monitor)
    zc = MagicMock()
    zc.zeroconf.cache.current_entry_with_name_and_alias.side_effect = lambda type_, _alias: (
        MagicMock() if type_ == "_esphomelib._tcp.local." else None
    )
    monitor.mdns._zeroconf = zc

    _dispatch_http_removed(monitor)

    assert device.runtime_state.state == DeviceState.ONLINE
    assert monitor.state.state_source["kitchen"] == ReachabilitySource.MDNS
    assert callbacks.calls_for("on_state_change") == []


async def test_http_removed_withdraws_when_yaml_gained_api_but_firmware_lacks_it() -> None:
    """The withdrawal keys on evidence, not the ``api_enabled`` YAML union."""
    device = make_online_api_device(loaded_integrations=["wifi"])
    assert device.api_enabled is True
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.MDNS
    _prime_removed(monitor)

    _dispatch_http_removed(monitor)

    assert device.runtime_state.state == DeviceState.UNKNOWN
    assert "kitchen" not in monitor.state.state_source


async def test_esphomelib_removed_defers_to_a_live_http_ptr() -> None:
    """Losing the esphomelib PTR re-anchors the election on a live http PTR."""
    device = make_online_api_device()
    monitor, callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.MDNS
    _prime_removed(monitor)
    zc = MagicMock()
    zc.zeroconf.cache.current_entry_with_name_and_alias.side_effect = lambda type_, _alias: (
        MagicMock() if type_ == "_http._tcp.local." else None
    )
    monitor.mdns._zeroconf = zc

    _dispatch_removed(monitor)

    assert device.runtime_state.state == DeviceState.ONLINE
    assert monitor.state.state_source["kitchen"] == ReachabilitySource.MDNS
    assert callbacks.calls_for("on_state_change") == []


async def test_http_removed_leaves_an_mqtt_owned_name_alone() -> None:
    """A name mdns doesn't own has nothing to withdraw; the broker's vouch stands."""
    device = make_online_api_device(api_enabled=False, loaded_integrations=["mqtt", "wifi"])
    monitor, callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.MQTT
    _prime_removed(monitor)

    _dispatch_http_removed(monitor)

    assert device.runtime_state.state == DeviceState.ONLINE
    assert monitor.state.state_source["kitchen"] == ReachabilitySource.MQTT
    assert callbacks.calls_for("on_state_change") == []


def test_anchor_ptr_elects_esphomelib_first_then_http() -> None:
    """The anchor election: esphomelib wins while live, else http, else nothing."""
    monitor, _callbacks = make_state_monitor_with_callbacks([make_online_api_device()])
    zc = MagicMock()
    monitor.mdns._zeroconf = zc
    lookup = zc.zeroconf.cache.current_entry_with_name_and_alias
    esphomelib_ptr = MagicMock()
    http_ptr = MagicMock()

    both = {"_esphomelib._tcp.local.": esphomelib_ptr, "_http._tcp.local.": http_ptr}
    for live, winner in [
        (both, esphomelib_ptr),
        ({"_http._tcp.local.": http_ptr}, http_ptr),
        ({"_esphomelib._tcp.local.": esphomelib_ptr}, esphomelib_ptr),
        ({}, None),
    ]:
        lookup.side_effect = lambda type_, _alias, _live=live: _live.get(type_)
        assert monitor.mdns._anchor_ptr("kitchen") is winner, live
        assert monitor.mdns.has_live_anchor_ptr("kitchen") is (winner is not None), live
