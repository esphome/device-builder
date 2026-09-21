"""Lock the ``DEVICE_STATE_CHANGED`` event payload shape.

The frontend (``DeviceStateChangedEventData``) destructures
``{configuration, state}`` flat. The backend used to fire
``{"device": <full Device>}``, which made both fields resolve to
``undefined`` and the device list never updated when ping (or any
other source) flipped a device online — exactly the bug from the
"Device comes online via ping but UI doesn't update" report.
"""

from __future__ import annotations

from typing import Any

import pytest

from esphome_device_builder.models import DeviceState, EventType, ReachabilitySource

from .conftest import make_device, make_devices_controller_with_bus


def test_state_change_event_uses_flat_configuration_state_payload() -> None:
    """``DEVICE_STATE_CHANGED`` carries flat ``configuration`` + ``state`` fields.

    Mirrors ``DeviceStateChangedEventData`` on the frontend — destructure
    expects ``{configuration, state}``, not ``{device: …}``. A regression
    that swaps them back makes every state transition no-op the UI.
    """
    device = make_device(address="")
    ctrl, captured = make_devices_controller_with_bus([device])

    ctrl._on_state_change("kitchen", DeviceState.ONLINE, "ping")

    assert [(e.event_type, e.data) for e in captured] == [
        (EventType.DEVICE_STATE_CHANGED, {"configuration": "kitchen.yaml", "state": "online"})
    ]


def test_state_change_state_value_is_serialised_string() -> None:
    """``state`` ships as the StrEnum ``.value`` string, not the enum object.

    The frontend treats it as an enum *member name* string and the JSON
    encoder for ``DeviceState`` serialises to the same form, but firing
    the bare enum would let an outer ``orjson.dumps`` choose its own
    encoding (or fail). Pin to ``.value`` so the wire format stays a
    plain string.
    """
    device = make_device(address="")
    ctrl, captured = make_devices_controller_with_bus([device])

    ctrl._on_state_change("kitchen", DeviceState.OFFLINE, "ping")

    assert len(captured) == 1
    assert captured[0].data["state"] == "offline"
    assert isinstance(captured[0].data["state"], str)


def test_state_change_unknown_device_does_not_fire() -> None:
    """A name not in the catalog is dropped — no spurious event."""
    ctrl, captured = make_devices_controller_with_bus([])

    ctrl._on_state_change("ghost", DeviceState.ONLINE, "mdns")

    assert captured == []


@pytest.mark.parametrize(
    ("devices", "source", "expected"),
    [
        pytest.param([{}], ReachabilitySource.MDNS, None, id="mdns_clears"),
        # Ping reaches the device through the record itself; it proves no name.
        pytest.param([{}], ReachabilitySource.PING, "asistente", id="ping_keeps"),
        # A same-path rename carries ``active_source`` forward as mdns.
        pytest.param(
            [{"active_source": ReachabilitySource.MDNS}],
            ReachabilitySource.MDNS,
            None,
            id="stale_active_source",
        ),
        # Siblings share one broadcast, so it can't prove which was flashed.
        pytest.param(
            [{}, {"configuration": "kitchen (1).yaml"}],
            ReachabilitySource.MDNS,
            "asistente",
            id="shared_name_keeps",
        ),
    ],
)
async def test_mdns_ownership_clears_the_deployed_name(
    devices: list[dict[str, Any]],
    source: ReachabilitySource,
    expected: str | None,
) -> None:
    """Only an mDNS announce that identifies one config proves the deployed name (#2730)."""
    rows = [make_device(address="", **kwargs) for kwargs in devices]
    ctrl, _ = make_devices_controller_with_bus(rows)
    ctrl._metadata_store.update("kitchen.yaml", deployed_name="asistente", delay=0.0)

    ctrl._on_source_change("kitchen", source)

    assert ctrl._metadata_store.get("kitchen.yaml").get("deployed_name") == expected


async def test_mdns_ownership_of_a_ping_online_device_still_clears() -> None:
    """``apply`` skips the state callback when the device is already ONLINE."""
    device = make_device(address="", state=DeviceState.ONLINE)
    ctrl, _ = make_devices_controller_with_bus([device])
    ctrl._metadata_store.update("kitchen.yaml", deployed_name="asistente", delay=0.0)

    ctrl._on_state_change("kitchen", DeviceState.ONLINE, "mdns")
    ctrl._on_source_change("kitchen", ReachabilitySource.MDNS)

    assert "deployed_name" not in ctrl._metadata_store.get("kitchen.yaml")
