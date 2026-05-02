"""Lock the ``DEVICE_STATE_CHANGED`` event payload shape.

The frontend (``DeviceStateChangedEventData``) destructures
``{configuration, state}`` flat. The backend used to fire
``{"device": <full Device>}``, which made both fields resolve to
``undefined`` and the device list never updated when ping (or any
other source) flipped a device online — exactly the bug from the
"Device comes online via ping but UI doesn't update" report.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from esphome_device_builder.controllers.devices import DevicesController
from esphome_device_builder.models import Device, DeviceState, EventType


def _make_controller(devices: list[Device]) -> tuple[DevicesController, MagicMock]:
    """Stand up enough of a controller to exercise ``_on_state_change``."""
    db = MagicMock()
    db.bus.fire = MagicMock()
    ctrl = DevicesController.__new__(DevicesController)
    ctrl._db = db  # type: ignore[attr-defined]
    ctrl._scanner = MagicMock()
    ctrl._scanner.devices = devices
    ctrl._scanner.get_by_name = lambda name, _d=devices: [d for d in _d if d.name == name]
    return ctrl, db


def test_state_change_event_uses_flat_configuration_state_payload() -> None:
    """``DEVICE_STATE_CHANGED`` carries flat ``configuration`` + ``state`` fields.

    Mirrors ``DeviceStateChangedEventData`` on the frontend — destructure
    expects ``{configuration, state}``, not ``{device: …}``. A regression
    that swaps them back makes every state transition no-op the UI.
    """
    device = Device(name="kitchen", friendly_name="Kitchen", configuration="kitchen.yaml")
    ctrl, db = _make_controller([device])

    ctrl._on_state_change("kitchen", DeviceState.ONLINE, "ping")

    db.bus.fire.assert_called_once_with(
        EventType.DEVICE_STATE_CHANGED,
        {"configuration": "kitchen.yaml", "state": "online"},
    )


def test_state_change_state_value_is_serialised_string() -> None:
    """``state`` ships as the StrEnum ``.value`` string, not the enum object.

    The frontend treats it as an enum *member name* string and the JSON
    encoder for ``DeviceState`` serialises to the same form, but firing
    the bare enum would let an outer ``orjson.dumps`` choose its own
    encoding (or fail). Pin to ``.value`` so the wire format stays a
    plain string.
    """
    device = Device(name="kitchen", friendly_name="Kitchen", configuration="kitchen.yaml")
    ctrl, db = _make_controller([device])

    ctrl._on_state_change("kitchen", DeviceState.OFFLINE, "ping")

    payload = db.bus.fire.call_args.args[1]
    assert payload["state"] == "offline"
    assert isinstance(payload["state"], str)


def test_state_change_unknown_device_does_not_fire() -> None:
    """A name not in the catalog is dropped — no spurious event."""
    ctrl, db = _make_controller([])

    ctrl._on_state_change("ghost", DeviceState.ONLINE, "mdns")

    db.bus.fire.assert_not_called()
