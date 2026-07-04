"""Tests for FirmwareController.stop() — bus listener teardown."""

from collections.abc import Iterator
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from esphome_device_builder.controllers.firmware.controller import FirmwareController
from esphome_device_builder.helpers.event_bus import EventBus
from esphome_device_builder.models import DeviceState, EventType


@pytest.fixture
def controller_with_real_bus() -> Iterator[FirmwareController]:
    """Wire a FirmwareController to a real EventBus.

    Bypasses the full __init__ DI graph (device_builder, devices, etc.)
    since stop() only needs to prove the actual add_listener/unsub
    round-trip works — mocking that mechanism would just test that we
    called a mock, not that the listener is actually gone.
    """
    controller = FirmwareController.__new__(FirmwareController)
    real_bus = EventBus()

    # Patch the class property for the duration of this fixture/test
    with patch.object(FirmwareController, "bus", new_callable=PropertyMock, return_value=real_bus):
        # Substitute the spy before subscribing so the bus stores *this*
        # callable — patching controller._handle_device_wake afterward
        # wouldn't change what's already captured in the listener set.
        controller._handle_device_wake = MagicMock()
        controller._unsub_device_wake = controller.bus.add_listener(
            EventType.DEVICE_STATE_CHANGED, controller._handle_device_wake
        )
        controller._handle_job_completed = MagicMock()
        controller._unsub_job_completed = controller.bus.add_listener(
            EventType.JOB_COMPLETED, controller._handle_job_completed
        )
        yield controller


def test_stop_unsubscribes_device_wake_listener(
    controller_with_real_bus: FirmwareController,
) -> None:
    """stop() must remove the DEVICE_STATE_CHANGED listener from the bus.

    Without this, a re-created controller (tests, a restart path) keeps
    a listener bound to a stale `self` receiving events forever.
    """
    controller = controller_with_real_bus
    payload = {"state": DeviceState.ONLINE.value, "configuration": "test_device.yaml"}

    # Sanity: the subscription is live before stop().
    controller.bus.fire(EventType.DEVICE_STATE_CHANGED, payload)
    controller._handle_device_wake.assert_called_once()

    controller.stop()

    # Firing again post-stop must not reach the handler at all — call
    # count stays at 1, not 2.
    controller.bus.fire(EventType.DEVICE_STATE_CHANGED, payload)
    controller._handle_device_wake.assert_called_once()


def test_stop_is_idempotent(controller_with_real_bus: FirmwareController) -> None:
    """Calling stop() twice must not raise.

    EventBus._remove_listener uses set.discard (no-op if already
    removed), so the unsub callable stays safe to call more than
    once — guards against a refactor that switches to something
    raise-on-missing (e.g. set.remove) without noticing this contract.
    """
    controller = controller_with_real_bus
    controller.stop()
    controller.stop()  # must not raise
