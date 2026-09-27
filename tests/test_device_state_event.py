"""Lock the ``DEVICE_STATE_CHANGED`` event payload shape.

The frontend (``DeviceStateChangedEventData``) destructures
``{configuration, state}`` flat. The backend used to fire
``{"device": <full Device>}``, which made both fields resolve to
``undefined`` and the device list never updated when ping (or any
other source) flipped a device online — exactly the bug from the
"Device comes online via ping but UI doesn't update" report.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from esphome_device_builder.controllers.devices import state_callbacks
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
        (
            EventType.DEVICE_STATE_CHANGED,
            {"configuration": "kitchen.yaml", "state": "online", "offline_seconds": None},
        )
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
    rows = [make_device(address="", deployed_name="asistente", **kwargs) for kwargs in devices]
    ctrl, _ = make_devices_controller_with_bus(rows)
    ctrl._metadata_store.update("kitchen.yaml", deployed_name="asistente", delay=0.0)

    ctrl._on_source_change("kitchen", source)

    assert ctrl._metadata_store.get("kitchen.yaml").get("deployed_name") == expected
    # Both readers consult the row, and no reload follows this clear.
    assert rows[0].deployed_name == (expected or "")


async def test_mdns_ownership_clears_a_row_the_store_already_lost() -> None:
    """An executor load can swap a pre-clear row in after the store was cleared."""
    row = make_device(address="", deployed_name="asistente")
    ctrl, _ = make_devices_controller_with_bus([row])

    ctrl._on_source_change("kitchen", ReachabilitySource.MDNS)

    assert row.deployed_name == ""


async def test_mdns_ownership_of_a_ping_online_device_still_clears() -> None:
    """``apply`` skips the state callback when the device is already ONLINE."""
    device = make_device(address="", state=DeviceState.ONLINE)
    ctrl, _ = make_devices_controller_with_bus([device])
    ctrl._metadata_store.update("kitchen.yaml", deployed_name="asistente", delay=0.0)

    ctrl._on_state_change("kitchen", DeviceState.ONLINE, "mdns")
    ctrl._on_source_change("kitchen", ReachabilitySource.MDNS)

    assert "deployed_name" not in ctrl._metadata_store.get("kitchen.yaml")


async def test_going_offline_stamps_the_outage() -> None:
    """Leaving ONLINE stores the stamp and fires its age, with no listing in between."""
    ctrl, captured = make_devices_controller_with_bus([make_device(state=DeviceState.ONLINE)])

    ctrl._on_state_change("kitchen", DeviceState.OFFLINE, "ping")

    stamp = ctrl._metadata_store.get_field("kitchen.yaml", "offline_since")
    assert abs(time.time() - stamp) < 5
    assert 0 <= captured[0].data["offline_seconds"] < 5


async def test_coming_back_online_clears_the_outage() -> None:
    """A device that is back has no outage on the model, the store or the event."""
    device = make_device(state=DeviceState.OFFLINE, offline_since=time.time() - 60)
    ctrl, captured = make_devices_controller_with_bus([device])
    ctrl._metadata_store.set_field("kitchen.yaml", "offline_since", time.time() - 60)

    ctrl._on_state_change("kitchen", DeviceState.ONLINE, "mdns")

    assert device.runtime_state.offline_since is None
    assert ctrl._metadata_store.get_field("kitchen.yaml", "offline_since") is None
    assert captured[0].data["offline_seconds"] is None


async def test_settling_offline_keeps_the_loaded_outage() -> None:
    """An UNKNOWN -> OFFLINE settle measures from the stamp the device loaded with."""
    since = time.time() - 7200
    ctrl, captured = make_devices_controller_with_bus([make_device(offline_since=since)])

    ctrl._on_state_change("kitchen", DeviceState.OFFLINE, "ping")

    assert ctrl._metadata_store.get_field("kitchen.yaml", "offline_since") == since
    assert abs(captured[0].data["offline_seconds"] - 7200) < 5


def test_settling_offline_without_an_outage_reports_none() -> None:
    """An already-offline device with no history reports no duration."""
    ctrl, captured = make_devices_controller_with_bus([make_device()])

    ctrl._on_state_change("kitchen", DeviceState.OFFLINE, "ping")

    assert ctrl._metadata_store.get_field("kitchen.yaml", "offline_since") is None
    assert captured[0].data["offline_seconds"] is None


async def test_an_observation_records_the_last_contact() -> None:
    """An observation stores when the device was last seen."""
    ctrl, _ = make_devices_controller_with_bus([make_device(state=DeviceState.ONLINE)])

    state_callbacks.record_last_seen(ctrl, "kitchen")

    assert abs(time.time() - ctrl._metadata_store.get_field("kitchen.yaml", "last_seen")) < 5


async def test_last_contact_writes_are_rate_limited() -> None:
    """A second observation moments later doesn't rewrite the stamp."""
    ctrl, _ = make_devices_controller_with_bus([make_device(state=DeviceState.ONLINE)])
    first = time.time() - 5
    ctrl._metadata_store.set_field("kitchen.yaml", "last_seen", first)

    state_callbacks.record_last_seen(ctrl, "kitchen")

    assert ctrl._metadata_store.get_field("kitchen.yaml", "last_seen") == first
