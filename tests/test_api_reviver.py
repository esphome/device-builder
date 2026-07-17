"""
Tests for the persisted-IP last-resort revival source.

A stuck-offline API device whose only lead is the on-disk last-known IP
must come back ONLINE only after the ICMP pre-filter answers AND a
Native API ``device_info`` round-trip confirms the responder's identity;
a mismatch instead invalidates the stale IP. The worker subprocess is
stubbed via ``_run_worker``, ICMP via ``ping_once``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.controllers._device_state_monitor import (
    api_reviver as api_reviver_module,
)
from esphome_device_builder.controllers._device_state_monitor._api_probe import ProbeError
from esphome_device_builder.controllers._device_state_monitor.api_reviver import (
    _DIAL_FAILURE_COOLDOWN,
    _DIAL_FAILURE_COOLDOWN_MAX,
    _ICMP_SILENT_COOLDOWN,
    _MAX_DIALS_PER_SWEEP,
    _NO_KEY_COOLDOWN,
    _VERIFIED_TTL,
    ApiReviverSource,
)
from esphome_device_builder.models import Device, DeviceState, ReachabilitySource

from .conftest import (
    RecordingMonitorCallbacks,
    make_state_monitor_with_callbacks,
    make_stuck_offline_device,
)

_WORKER_MATCH = {
    "name": "kitchen",
    "mac_address": "94c9601f8cf1",
    "esphome_version": "2026.7.0",
}


def _reviver(
    devices: list[Device],
    *,
    worker_result: dict[str, Any] | None = _WORKER_MATCH,
    rtt: float | None = 12.5,
) -> tuple[Any, RecordingMonitorCallbacks, ApiReviverSource]:
    """Monitor + reviver with ICMP available, DNS failure cached, worker stubbed."""
    monitor, callbacks = make_state_monitor_with_callbacks(devices)
    monitor.state.dns_cache = MagicMock()
    monitor.state.dns_cache.has_cached_failure.return_value = True
    monitor.ping.icmp_available = True
    monitor.ping.ping_once = AsyncMock(return_value=rtt)  # type: ignore[method-assign]
    src = monitor.api_reviver
    src._run_worker = AsyncMock(return_value=worker_result)  # type: ignore[method-assign]
    return monitor, callbacks, src


def _cooldown_delta(src: ApiReviverSource, device: Device) -> float:
    return src._cooldown.remaining((device.name, device.ip))


# ----------------------------------------------------------------------
# Revival — identity match
# ----------------------------------------------------------------------


async def test_match_revives_online_under_ping() -> None:
    """Full match: IP reseeded before the state flip, ONLINE via ping, sweep woken."""
    device = make_stuck_offline_device()
    monitor, callbacks, src = _reviver([device])

    await src._sweep()

    names = [call[0] for call in callbacks.calls]
    assert ("on_ip_change", "kitchen", "192.168.1.50", ["192.168.1.50"]) in callbacks.calls
    assert ("on_state_change", "kitchen", DeviceState.ONLINE, "ping") in callbacks.calls
    assert names.index("on_ip_change") < names.index("on_state_change")
    assert device.runtime_state.state is DeviceState.ONLINE
    assert monitor.priority_for("kitchen") is ReachabilitySource.PING
    verified_ip, _verified_at = src._verified["kitchen"]
    assert verified_ip == "192.168.1.50"
    assert monitor.ping._wake.is_set()


async def test_match_applies_mac_and_version_from_the_same_dial() -> None:
    device = make_stuck_offline_device()
    _monitor, callbacks, src = _reviver([device])

    await src._sweep()

    assert ("on_mac_address_change", "kitchen", "94:C9:60:1F:8C:F1") in callbacks.calls
    assert ("on_version_change", "kitchen", "2026.7.0") in callbacks.calls


async def test_match_stamps_deployed_identity_live() -> None:
    """The verified dial is first-party identity evidence."""
    device = make_stuck_offline_device()
    _monitor, callbacks, src = _reviver([device])

    await src._sweep()

    assert device.runtime_state.deployed_identity_live is True
    assert ("on_deployed_identity_live_change", "kitchen", True) in callbacks.calls


async def test_name_mismatch_does_not_stamp_deployed_identity_live() -> None:
    """A stranger answering the persisted IP proves nothing about our device."""
    device = make_stuck_offline_device()
    _monitor, callbacks, src = _reviver(
        [device], worker_result={**_WORKER_MATCH, "name": "stranger"}
    )

    await src._sweep()

    assert device.runtime_state.deployed_identity_live is False
    assert callbacks.calls_for("on_deployed_identity_live_change") == []


async def test_match_with_blank_persisted_mac_still_claims() -> None:
    """Name alone suffices when either MAC side is unknown."""
    device = make_stuck_offline_device(mac_address="")
    _monitor, callbacks, src = _reviver([device])

    await src._sweep()

    assert ("on_state_change", "kitchen", DeviceState.ONLINE, "ping") in callbacks.calls


async def test_unknown_state_device_is_eligible() -> None:
    device = make_stuck_offline_device(state=DeviceState.UNKNOWN)
    _monitor, callbacks, src = _reviver([device])

    await src._sweep()

    assert ("on_state_change", "kitchen", DeviceState.ONLINE, "ping") in callbacks.calls


async def test_mdns_takes_over_after_revival() -> None:
    device = make_stuck_offline_device()
    monitor, _callbacks, src = _reviver([device])
    await src._sweep()

    monitor.apply("kitchen", DeviceState.ONLINE, "mdns", claim=True)

    assert monitor.priority_for("kitchen") is ReachabilitySource.MDNS


# ----------------------------------------------------------------------
# ICMP pre-filter — negative gate, never ONLINE evidence alone
# ----------------------------------------------------------------------


async def test_icmp_silence_skips_the_dial_and_cools_down() -> None:
    device = make_stuck_offline_device()
    _monitor, callbacks, src = _reviver([device], rtt=None)

    await src._sweep()

    src._run_worker.assert_not_called()
    assert callbacks.calls == []
    assert 0 < _cooldown_delta(src, device) <= _ICMP_SILENT_COOLDOWN


async def test_run_exits_when_icmp_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Without a trustworthy negative pre-filter the reviver refuses to run at all."""
    device = make_stuck_offline_device()
    monitor, _callbacks, src = _reviver([device])
    monitor.ping.icmp_available = False
    monkeypatch.setattr(ApiReviverSource, "_bootstrap_delay", 0)

    with caplog.at_level(logging.WARNING):
        await asyncio.wait_for(src.run(), timeout=1)

    assert "API revival disabled" in caplog.text
    monitor.ping.ping_once.assert_not_called()


async def test_prepare_requires_the_worker_library(monkeypatch: pytest.MonkeyPatch) -> None:
    """No aioesphomeapi means no identity dial — the source disables itself."""
    device = make_stuck_offline_device()
    _monitor, _callbacks, src = _reviver([device])
    monkeypatch.setattr(api_reviver_module, "api_worker_available", lambda: False)

    assert await src._prepare() is False


async def test_prepare_passes_with_worker_and_icmp() -> None:
    device = make_stuck_offline_device()
    _monitor, _callbacks, src = _reviver([device])

    assert await src._prepare() is True


# ----------------------------------------------------------------------
# Cohort — probe only when no other reachability signal exists
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"state": DeviceState.ONLINE}, id="already_online"),
        pytest.param({"api_enabled": False, "loaded_integrations": ["wifi"]}, id="no_api"),
        pytest.param({"ip": ""}, id="never_seen_no_persisted_ip"),
        pytest.param({"ip_addresses": ["192.168.1.50"]}, id="ram_addresses_are_pings_job"),
    ],
)
async def test_cohort_skips(overrides: dict[str, Any]) -> None:
    device = make_stuck_offline_device(**overrides)
    monitor, _callbacks, src = _reviver([device])

    await src._sweep()

    monitor.ping.ping_once.assert_not_called()
    src._run_worker.assert_not_called()


async def test_mid_process_mdns_death_enters_the_cohort_and_revives() -> None:
    """A confirmed ``Removed`` keeps the last-known ``ip``, so no restart is needed."""
    device = make_stuck_offline_device(
        state=DeviceState.ONLINE, ip_addresses=["192.168.1.50", "fe80::1"]
    )
    monitor, callbacks, src = _reviver([device])
    monitor.apply("kitchen", DeviceState.ONLINE, "mdns", claim=True)

    # The confirmed-Removed branch in ``mdns._verify_removed``.
    monitor.apply("kitchen", DeviceState.OFFLINE, "mdns")
    monitor.clear_resolved_addresses("kitchen")
    monitor.forget("kitchen")
    assert device.ip == "192.168.1.50"
    assert device.runtime_state.ip_addresses == []

    await src._sweep()

    src._run_worker.assert_awaited_once()
    assert device.runtime_state.state is DeviceState.ONLINE
    assert ("on_state_change", "kitchen", DeviceState.ONLINE, "ping") in callbacks.calls
    assert device.runtime_state.ip_addresses == ["192.168.1.50"]


async def test_cohort_skips_without_a_cached_dns_failure() -> None:
    """No cached failure yet means ping hasn't proven it has no target."""
    device = make_stuck_offline_device()
    monitor, _callbacks, src = _reviver([device])
    monitor.state.dns_cache.has_cached_failure.return_value = False

    await src._sweep()

    monitor.ping.ping_once.assert_not_called()


async def test_cohort_skips_when_zeroconf_cache_has_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = make_stuck_offline_device()
    monitor, _callbacks, src = _reviver([device])
    monkeypatch.setattr(monitor.mdns, "get_cached_addresses", lambda _host: ["192.168.1.50"])

    await src._sweep()

    monitor.ping.ping_once.assert_not_called()


async def test_cohort_includes_device_with_blank_address() -> None:
    device = make_stuck_offline_device(address="")
    monitor, _callbacks, src = _reviver([device])
    monitor.state.dns_cache.has_cached_failure.return_value = False

    await src._sweep()

    monitor.ping.ping_once.assert_awaited_once_with("192.168.1.50", retry=True)


async def test_post_revival_flap_is_pings_to_demote_and_keep() -> None:
    """A later ping OFFLINE leaves RAM addresses, so the reviver stays out."""
    device = make_stuck_offline_device()
    monitor, _callbacks, src = _reviver([device])
    await src._sweep()
    src._run_worker.reset_mock()
    monitor.ping.ping_once.reset_mock()

    monitor.apply("kitchen", DeviceState.OFFLINE, "ping")
    assert device.runtime_state.ip_addresses == ["192.168.1.50"]
    await src._sweep()

    monitor.ping.ping_once.assert_not_called()
    src._run_worker.assert_not_called()


# ----------------------------------------------------------------------
# Verified cache — at most one dial per stuck device per process
# ----------------------------------------------------------------------


async def test_second_episode_revives_without_a_dial() -> None:
    """MDNS Removed cleared the RAM addresses; the verified pair revives ICMP-only."""
    device = make_stuck_offline_device()
    _monitor, callbacks, src = _reviver([device])
    await src._sweep()
    src._run_worker.reset_mock()

    device.runtime_state.state = DeviceState.OFFLINE
    device.runtime_state.ip_addresses = []
    await src._sweep()

    src._run_worker.assert_not_called()
    assert device.runtime_state.state is DeviceState.ONLINE
    assert callbacks.calls_for("on_state_change")[-1] == (
        "on_state_change",
        "kitchen",
        DeviceState.ONLINE,
        "ping",
    )


async def test_persisted_ip_change_invalidates_the_verified_pair() -> None:
    device = make_stuck_offline_device()
    _monitor, _callbacks, src = _reviver([device])
    src._verified["kitchen"] = ("192.168.1.99", time.monotonic())

    await src._sweep()

    src._run_worker.assert_awaited_once()
    assert src._verified["kitchen"][0] == "192.168.1.50"


async def test_stale_verification_re_dials() -> None:
    """Past the TTL the echo alone no longer revives; identity is re-verified."""
    device = make_stuck_offline_device()
    _monitor, _callbacks, src = _reviver([device])
    src._verified["kitchen"] = ("192.168.1.50", time.monotonic() - _VERIFIED_TTL - 1)

    await src._sweep()

    src._run_worker.assert_awaited_once()
    verified_ip, verified_at = src._verified["kitchen"]
    assert verified_ip == "192.168.1.50"
    assert time.monotonic() - verified_at <= _VERIFIED_TTL


# ----------------------------------------------------------------------
# Identity mismatch — stale persisted IP
# ----------------------------------------------------------------------


async def test_name_mismatch_invalidates_the_persisted_ip() -> None:
    device = make_stuck_offline_device()
    monitor, callbacks, src = _reviver(
        [device], worker_result={**_WORKER_MATCH, "name": "stranger"}
    )

    await src._sweep()

    assert ("on_persisted_ip_invalidated", "kitchen", "192.168.1.50") in callbacks.calls
    assert callbacks.calls_for("on_state_change") == []
    assert device.ip == ""
    assert src._verified == {}
    # Backoff recorded independently of the callback, so an unwired
    # monitor still never re-dials the stranger every sweep.
    assert not src._cooldown.ready(("kitchen", "192.168.1.50"))

    # The cleared IP fails the cohort gate: no re-dial next sweep.
    src._run_worker.reset_mock()
    monitor.ping.ping_once.reset_mock()
    await src._sweep()
    monitor.ping.ping_once.assert_not_called()
    src._run_worker.assert_not_called()


async def test_empty_reported_name_is_inconclusive_not_a_mismatch() -> None:
    """A payload with no identity backs off; it never clears the revival lead."""
    device = make_stuck_offline_device()
    _monitor, callbacks, src = _reviver([device], worker_result={**_WORKER_MATCH, "name": ""})

    await src._sweep()

    assert callbacks.calls_for("on_persisted_ip_invalidated") == []
    assert callbacks.calls_for("on_state_change") == []
    assert device.ip == "192.168.1.50"
    assert 0 < _cooldown_delta(src, device) <= _DIAL_FAILURE_COOLDOWN


async def test_mac_conflict_neither_claims_nor_invalidates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Same name but a different MAC could be swapped hardware — hold everything."""
    device = make_stuck_offline_device(mac_address="AA:BB:CC:DD:EE:FF")
    _monitor, callbacks, src = _reviver([device])

    with caplog.at_level(logging.WARNING):
        await src._sweep()

    assert "not claiming ONLINE" in caplog.text
    assert callbacks.calls_for("on_state_change") == []
    assert callbacks.calls_for("on_persisted_ip_invalidated") == []
    assert device.ip == "192.168.1.50"
    assert 0 < _cooldown_delta(src, device) <= _DIAL_FAILURE_COOLDOWN


# ----------------------------------------------------------------------
# Dial politeness — failures, caps, cooldowns
# ----------------------------------------------------------------------


async def test_dial_failure_backs_off_with_escalation() -> None:
    device = make_stuck_offline_device()
    _monitor, callbacks, src = _reviver([device], worker_result=None)

    await src._sweep()
    assert callbacks.calls == []
    first = _cooldown_delta(src, device)
    assert 0 < first <= _DIAL_FAILURE_COOLDOWN

    src._cooldown.set((device.name, device.ip), 0)
    await src._sweep()
    second = _cooldown_delta(src, device)
    assert first < second <= 2 * _DIAL_FAILURE_COOLDOWN

    for _ in range(10):
        src._record_dial_failure((device.name, device.ip))
    assert _cooldown_delta(src, device) <= _DIAL_FAILURE_COOLDOWN_MAX


async def test_encrypted_device_without_key_is_not_dialed() -> None:
    device = make_stuck_offline_device(api_encrypted=True)
    _monitor, _callbacks, src = _reviver([device])

    await src._sweep()

    src._run_worker.assert_not_called()
    assert 0 < _cooldown_delta(src, device) <= _NO_KEY_COOLDOWN


async def test_host_side_worker_miss_retries_soon() -> None:
    """A spawn/timeout blip says nothing about the device; no escalation."""
    device = make_stuck_offline_device()
    _monitor, _callbacks, src = _reviver([device])
    src._run_worker = AsyncMock(side_effect=ProbeError("worker timed out", transient=True))

    await src._sweep()

    assert 0 < _cooldown_delta(src, device) <= _ICMP_SILENT_COOLDOWN
    assert src._cooldown.strikes((device.name, device.ip)) == 0


async def test_transient_resolve_failure_retries_soon() -> None:
    """A key/port resolve blip gets the short ICMP-miss cooldown, not the no-key hold."""
    device = make_stuck_offline_device()
    monitor, _callbacks, src = _reviver([device])
    monitor._resolve_api_connection = AsyncMock(side_effect=RuntimeError("resolve boom"))

    await src._sweep()

    src._run_worker.assert_not_called()
    assert 0 < _cooldown_delta(src, device) <= _ICMP_SILENT_COOLDOWN
    assert src._cooldown.strikes((device.name, device.ip)) == 0


async def test_duplicate_name_siblings_sharing_an_ip_dial_once() -> None:
    """One dial answers for the whole name bucket; no double slot pressure or double strike."""
    first = make_stuck_offline_device()
    second = make_stuck_offline_device()
    second.configuration = "kitchen (1).yaml"
    _monitor, _callbacks, src = _reviver([first, second], worker_result=None)

    await src._sweep()

    assert src._run_worker.await_count == 1
    assert src._cooldown.strikes(("kitchen", "192.168.1.50")) == 1


async def test_pair_mutated_between_prefilter_and_dial_is_skipped() -> None:
    """An IP invalidated or re-learned mid-sweep voids what the echo proved."""
    device = make_stuck_offline_device()
    monitor, callbacks, src = _reviver([device])

    async def clear_ip(_ip: str, *, retry: bool) -> float:
        device.ip = ""
        return 12.5

    monitor.ping.ping_once = clear_ip  # type: ignore[method-assign]
    await src._sweep()

    src._run_worker.assert_not_called()
    assert callbacks.calls_for("on_state_change") == []


async def test_mid_dial_mdns_address_set_is_not_overwritten() -> None:
    """A fresher mDNS-learned address set that lands mid-dial survives the revive."""
    device = make_stuck_offline_device()
    _monitor, callbacks, src = _reviver([device])

    async def worker_while_mdns_lands(_device: Device, _request: bytes) -> dict[str, str]:
        device.runtime_state.ip_addresses = ["192.168.1.77", "fe80::1"]
        return dict(_WORKER_MATCH)

    src._run_worker = worker_while_mdns_lands  # type: ignore[method-assign]
    await src._sweep()

    assert callbacks.calls_for("on_ip_change") == []
    assert device.runtime_state.ip_addresses == ["192.168.1.77", "fe80::1"]
    assert ("on_state_change", "kitchen", DeviceState.ONLINE, "ping") in callbacks.calls


async def test_worker_dials_share_one_monitor_wide_slot() -> None:
    """Reviver and api_info dials serialize on the global budget, never two subprocesses."""
    device = make_stuck_offline_device()
    monitor, _callbacks, src = _reviver([device])
    in_flight = 0
    peak = 0

    async def slow_worker(_device: Device, _request: bytes) -> None:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1

    src._run_worker = slow_worker  # type: ignore[method-assign]
    monitor.api_info._run_worker = slow_worker  # type: ignore[method-assign]

    await asyncio.gather(
        src._probe(device, [device.ip]),
        monitor.api_info._probe(device, [device.ip]),
    )

    assert peak == 1


async def test_dials_are_capped_per_sweep_and_overflow_rolls() -> None:
    devices = [make_stuck_offline_device(f"dev{i}", ip=f"192.168.1.{50 + i}") for i in range(5)]
    _monitor, _callbacks, src = _reviver(devices, worker_result=None)

    await src._sweep()

    assert src._run_worker.await_count == _MAX_DIALS_PER_SWEEP
    # The un-dialed overflow is not cooled down — it rolls to the next sweep.
    assert len(src._cooldown) == _MAX_DIALS_PER_SWEEP


# ----------------------------------------------------------------------
# Bookkeeping — prune / reset
# ----------------------------------------------------------------------


async def test_online_transition_resets_the_backoff() -> None:
    device = make_stuck_offline_device()
    _monitor, _callbacks, src = _reviver([device], worker_result=None)
    await src._sweep()
    key = (device.name, device.ip)
    assert key in src._cooldown
    assert src._cooldown.strikes(key) == 1

    device.runtime_state.state = DeviceState.ONLINE
    await src._sweep()

    assert not src._cooldown
    assert src._cooldown.strikes(key) == 0


async def test_prune_keeps_a_shadowed_duplicate_names_bookkeeping() -> None:
    """Two YAMLs sharing a name with different persisted IPs each keep their cooldowns."""
    first = make_stuck_offline_device(ip="192.168.1.50")
    second = make_stuck_offline_device(ip="192.168.1.60")
    second.configuration = "kitchen (1).yaml"
    _monitor, _callbacks, src = _reviver([first, second], rtt=None)

    await src._sweep()
    cooled_before = dict(src._cooldown._deadline)
    await src._sweep()

    assert ("kitchen", "192.168.1.50") in cooled_before
    assert ("kitchen", "192.168.1.60") in cooled_before
    assert src._cooldown._deadline == cooled_before


async def test_removed_device_drops_all_bookkeeping() -> None:
    device = make_stuck_offline_device()
    devices = [device]
    _monitor, _callbacks, src = _reviver(devices, worker_result=None)
    await src._sweep()
    src._verified["kitchen"] = ("192.168.1.50", time.monotonic())

    devices.clear()
    await src._sweep()

    assert not src._cooldown
    assert src._verified == {}
