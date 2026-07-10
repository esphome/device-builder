"""``Device.to_dict`` projection benchmarks at fleet sizes 50 / 200."""

from __future__ import annotations

import pytest
from pytest_codspeed import BenchmarkFixture

from esphome_device_builder.models import Device, DeviceRuntimeState, DeviceState


def _make_devices(n: int) -> list[Device]:
    """Build *n* representative ``Device`` instances with realistic field coverage."""
    devices: list[Device] = []
    for index in range(n):
        name = f"device_{index:04d}"
        devices.append(
            Device(
                name=name,
                friendly_name=f"Device {index:04d}",
                configuration=f"{name}.yaml",
                comment=f"Synthetic device {index} for benchmark",
                area=f"Bench Room {index % 10}",
                board_id="esp32-c3-devkitm-1",
                target_platform="esp32",
                address=f"{name}.local",
                ip=f"192.168.1.{index % 250 + 2}",
                web_port=None,
                current_version="2026.5.0",
                expected_config_hash=f"{index:08x}",
                runtime_state=DeviceRuntimeState(
                    state=DeviceState.ONLINE,
                    ip_addresses=[f"192.168.1.{index % 250 + 2}", f"fe80::{index:x}%wlan0"],
                    deployed_version="2026.5.0",
                ),
            )
        )
    return devices


@pytest.mark.parametrize("fleet_size", [50, 200])
def test_device_list_to_dict_fleet(
    benchmark: BenchmarkFixture,
    fleet_size: int,
) -> None:
    """Device-list ``to_dict`` projection cost at fleet size N (first-paint slice)."""
    devices = _make_devices(fleet_size)

    warm = [d.to_dict() for d in devices]
    assert len(warm) == fleet_size
    assert warm[0]["name"] == "device_0000"

    @benchmark
    def run() -> None:
        [d.to_dict() for d in devices]
