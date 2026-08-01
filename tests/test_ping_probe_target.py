"""Coverage for ``PingSource.probe_target``."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from esphome_device_builder.controllers._device_state_monitor.ping import PingSource
from esphome_device_builder.models import Device, DeviceRuntimeState, DeviceState


def _device(state: DeviceState) -> Device:
    return Device(
        name="kitchen",
        friendly_name="Kitchen",
        configuration="kitchen.yaml",
        runtime_state=DeviceRuntimeState(state=state),
    )


async def test_probe_target_applies_and_retries_like_the_sweep() -> None:
    monitor = MagicMock()
    ping = PingSource(monitor)
    ping.ping_once = AsyncMock(return_value=4.2)  # type: ignore[method-assign]

    rtt = await ping.probe_target(_device(DeviceState.UNKNOWN), "10.0.0.42")

    assert rtt == 4.2
    ping.ping_once.assert_awaited_once_with("10.0.0.42", retry=True)
    monitor.apply.assert_called_once_with("kitchen", DeviceState.ONLINE, "ping")


async def test_probe_target_offline_single_packet_and_apply_opt_out() -> None:
    monitor = MagicMock()
    ping = PingSource(monitor)
    ping.ping_once = AsyncMock(return_value=None)  # type: ignore[method-assign]

    rtt = await ping.probe_target(_device(DeviceState.OFFLINE), "10.0.0.42", apply=False)

    assert rtt is None
    ping.ping_once.assert_awaited_once_with("10.0.0.42", retry=False)
    monitor.apply.assert_not_called()
