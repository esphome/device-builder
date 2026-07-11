"""Tests for ``DevicesController.probe_reachability_if_unknown``."""

from __future__ import annotations

from pathlib import Path

import pytest

from esphome_device_builder.controllers.devices import DevicesController
from esphome_device_builder.models import DeviceState
from tests.conftest import make_device
from tests.controllers.devices.conftest import MakeControllerFactory


def _controller_with_device(
    make_controller: MakeControllerFactory, tmp_path: Path, state: DeviceState
) -> DevicesController:
    controller = make_controller(tmp_path, with_state_monitor=True)
    controller._scanner.devices = [make_device("kitchen", state=state)]
    return controller


def test_probe_fires_for_unknown_device(
    make_controller: MakeControllerFactory, tmp_path: Path
) -> None:
    """An UNKNOWN device gets both the mDNS resolve and the ICMP sweep wake."""
    controller = _controller_with_device(make_controller, tmp_path, DeviceState.UNKNOWN)

    controller.probe_reachability_if_unknown("kitchen.yaml")

    assert controller._state_monitor.calls == [
        ("probe_device", "kitchen", None),
        ("probe_device_ping", "kitchen"),
    ]


@pytest.mark.parametrize("state", [DeviceState.ONLINE, DeviceState.OFFLINE])
def test_probe_skips_settled_device(
    make_controller: MakeControllerFactory, tmp_path: Path, state: DeviceState
) -> None:
    """Settled devices stay on the normal cadence."""
    controller = _controller_with_device(make_controller, tmp_path, state)

    controller.probe_reachability_if_unknown("kitchen.yaml")

    assert controller._state_monitor.calls == []


def test_probe_skips_missing_device(make_controller: MakeControllerFactory, tmp_path: Path) -> None:
    """An unknown configuration is a no-op."""
    controller = make_controller(tmp_path, with_state_monitor=True)

    controller.probe_reachability_if_unknown("ghost.yaml")

    assert controller._state_monitor.calls == []
