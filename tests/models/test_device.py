"""``Device`` serialisation contracts."""

from __future__ import annotations

from esphome_device_builder.models import DeviceState

from ..conftest import make_device


def test_to_flat_dict_lifts_runtime_state_to_the_top_level() -> None:
    device = make_device("kitchen", state=DeviceState.ONLINE, deployed_version="2026.8.2")
    flat = device.to_flat_dict()
    assert "runtime_state" not in flat
    assert flat["state"] == "online"
    assert flat["deployed_version"] == "2026.8.2"
    assert flat["configuration"] == "kitchen.yaml"
