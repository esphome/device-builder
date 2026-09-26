"""End-to-end coverage for the listing-flavoured ``DevicesController`` commands.

The ``devices/list`` and ``devices/get_states`` commands are the
dashboard's poll-on-page-load surface — every dashboard tab and
every reconnect goes through them. They were uncovered by the
existing suite because most tests reach into ``self._scanner.devices``
directly rather than invoking the public commands. Pin them so a
refactor that drops the ``await self._scanner.scan()`` warm-up (or
that swaps the configured-vs-importable filter shape) shows up as
a failure here.

Same shape for ``get_devices`` (the sync snapshot used by the
state monitor) and ``get_importable_devices`` (the
``initial_state`` seed for new WS clients) — both paths are the
controller-side glue between the scanner's index and the
dashboard's rendering layer.

The ``_on_importable_added`` / ``_on_importable_removed`` callbacks
that maintain ``import_result`` are exercised through the same
``get_importable_devices`` test, but pinned independently so a
regression that fires the wrong event type (or skips firing
entirely) surfaces here rather than as a phantom-card bug in
production.
"""

from __future__ import annotations

import time
from pathlib import Path

from esphome_device_builder.models import (
    AdoptableDevice,
    Device,
    DevicesResponse,
    DeviceState,
    EventType,
)
from tests.conftest import make_device

from .conftest import CaptureDevicesEventsFactory, MakeControllerFactory


def _device(name: str, *, state: DeviceState = DeviceState.ONLINE) -> Device:
    return make_device(name=name, state=state)


def _adoptable(name: str = "kitchen-1a2b3c") -> AdoptableDevice:
    """Bare-minimum ``AdoptableDevice`` for importable assertions."""
    return AdoptableDevice(
        name=name,
        friendly_name="Kitchen",
        package_import_url="github://acme/firmware/kitchen.yaml@main",
        project_name="acme.kitchen",
        project_version="2026.05.01",
        network="wifi",
        ignored=False,
    )


def _seed(controller: object, *devices: Device) -> None:
    """Put *devices* in both the scanner's list and its name index."""
    controller._scanner.devices = list(devices)
    controller._scanner._devices_by_name = {d.name: [d] for d in devices}


# ---------------------------------------------------------------------------
# get_devices / get_device_states / list_devices
# ---------------------------------------------------------------------------


def test_get_devices_returns_scanner_snapshot(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """``get_devices`` is the sync bridge for the state monitor.

    The state monitor reads the device list to fan out per-name
    state changes; the callback is sync so it can't ``await`` a
    scan. Pin that ``get_devices`` returns the scanner's current
    snapshot directly — a regression that scheduled a fresh scan
    here would deadlock the monitor's callback chain.
    """
    controller = make_controller(tmp_path)
    controller._scanner.devices = [_device("kitchen"), _device("bedroom")]

    snapshot = controller.get_devices()

    assert [d.name for d in snapshot] == ["kitchen", "bedroom"]


def test_get_by_configuration_resolves_a_device_by_filename(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """The controller delegates a filename lookup to the scanner, ``None`` if unknown."""
    controller = make_controller(tmp_path)
    kitchen = _device("kitchen")
    controller._scanner.devices = [kitchen, _device("bedroom")]

    assert controller.get_by_configuration("kitchen.yaml") is kitchen
    assert controller.get_by_configuration("ghost.yaml") is None


async def test_get_device_states_returns_configuration_keyed_map(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """``devices/get_states`` keys by ``configuration`` (the filename), not name.

    Two YAMLs can share an ``esphome.name`` (``foo.yaml`` and
    ``foo (1).yaml``) — keying by configuration is the only way
    the response stays unambiguous. Pin the configuration-key
    shape so a refactor that dropped to ``name`` keys (silent in
    the single-file case, broken for the duplicate case) fails
    here.
    """
    controller = make_controller(tmp_path)
    controller._scanner.devices = [
        _device("kitchen", state=DeviceState.ONLINE),
        _device("bedroom", state=DeviceState.OFFLINE),
    ]

    states = await controller.get_device_states()

    assert states == {"kitchen.yaml": "online", "bedroom.yaml": "offline"}


async def test_list_devices_scans_then_returns_configured_and_importable(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """``devices/list`` triggers a scan and returns a ``DevicesResponse``.

    The scan-then-list shape is what makes the dashboard's
    initial render include freshly-dropped YAML files (e.g.
    a ``git pull`` between page loads) — without the explicit
    scan, the listing reads stale state from the last poll.
    """
    controller = make_controller(tmp_path)
    controller._scanner.devices = [_device("kitchen")]
    controller.state.import_result = {"kitchen-1a2b3c": _adoptable()}

    response = await controller.list_devices()

    assert isinstance(response, DevicesResponse)
    assert [d.name for d in response.configured] == ["kitchen"]
    assert [d.name for d in response.importable] == ["kitchen-1a2b3c"]
    # Scanner was kicked once before the listing.
    assert ("scan", False) in controller._scanner.calls


async def test_list_devices_filters_importable_already_configured(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """An importable device with the same name as a configured one is hidden.

    Pre-existing filter that catches the race where a YAML
    appeared between the discovery callback firing and the user
    refreshing the page. Without the filter the user sees a
    duplicate "Adopt" card for a device they already adopted.
    """
    controller = make_controller(tmp_path)
    controller._scanner.devices = [_device("kitchen-1a2b3c")]
    # Same name as the configured device — should be filtered out.
    controller.state.import_result = {"kitchen-1a2b3c": _adoptable("kitchen-1a2b3c")}

    response = await controller.list_devices()

    assert response.importable == []


# ---------------------------------------------------------------------------
# importable lifecycle — _on_importable_added / _on_importable_removed
# ---------------------------------------------------------------------------


def test_on_importable_added_stashes_and_fires_event(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    capture_devices_events: CaptureDevicesEventsFactory,
) -> None:
    """``import_result`` is keyed by ``device.name`` and fires DEVICE_ADDED.

    The dashboard's discovered-cards panel listens for
    ``IMPORTABLE_DEVICE_ADDED`` to render a fresh card without
    waiting for the next ``devices/list`` poll. Pin the
    name-keyed cache shape — anything else (e.g. service-instance
    keying) breaks the ``devices/ignore`` flow which addresses
    entries by ``name``.
    """
    controller = make_controller(tmp_path)
    controller.state.import_result = {}
    captured = capture_devices_events(controller, EventType.IMPORTABLE_DEVICE_ADDED)
    adoptable = _adoptable()

    controller._on_importable_added(adoptable)

    assert controller.state.import_result == {"kitchen-1a2b3c": adoptable}
    assert len(captured) == 1
    assert captured[0].event_type is EventType.IMPORTABLE_DEVICE_ADDED
    assert captured[0].data == {"device": adoptable}


def test_on_importable_removed_drops_entry_and_fires_event(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    capture_devices_events: CaptureDevicesEventsFactory,
) -> None:
    """``IMPORTABLE_DEVICE_REMOVED`` carries just the name, not the full record.

    The frontend keys its discovered-card list by name, so the
    removed event only needs the name. Pin the payload shape and
    that the cache entry actually goes away.
    """
    controller = make_controller(tmp_path)
    controller.state.import_result = {"kitchen-1a2b3c": _adoptable()}
    captured = capture_devices_events(controller, EventType.IMPORTABLE_DEVICE_REMOVED)

    controller._on_importable_removed("kitchen-1a2b3c")

    assert controller.state.import_result == {}
    assert len(captured) == 1
    assert captured[0].event_type is EventType.IMPORTABLE_DEVICE_REMOVED
    assert captured[0].data == {"name": "kitchen-1a2b3c"}


def test_on_importable_removed_ignores_unknown_name(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    capture_devices_events: CaptureDevicesEventsFactory,
) -> None:
    """An unknown name is a no-op — no cache pop, no event.

    mDNS can reorder ``Removed`` events around our own pop
    (e.g. a user adopting a device immediately before its mDNS
    record expires). Firing a phantom ``REMOVED`` for an entry
    we never added would make the frontend re-render the cards
    panel for nothing.
    """
    controller = make_controller(tmp_path)
    controller.state.import_result = {}
    captured = capture_devices_events(controller, EventType.IMPORTABLE_DEVICE_REMOVED)

    controller._on_importable_removed("never-seen")

    assert captured == []


def test_get_importable_devices_filters_already_configured(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """The ``initial_state`` seed strips out names that already have a YAML.

    A device that was adopted without its mDNS record being
    removed (the device kept announcing on its old name) would
    otherwise leak into the seed a fresh page load gets, showing
    up as a phantom adoption card the user can't dismiss.
    """
    controller = make_controller(tmp_path)
    controller._scanner.devices = [_device("kitchen-1a2b3c")]
    controller.state.import_result = {
        "kitchen-1a2b3c": _adoptable("kitchen-1a2b3c"),
        "bedroom-d4e5f6": _adoptable("bedroom-d4e5f6"),
    }

    seed = controller.get_importable_devices()

    assert [d.name for d in seed] == ["bedroom-d4e5f6"]


# ---------------------------------------------------------------------------
# offline duration
# ---------------------------------------------------------------------------


async def test_list_devices_reports_offline_duration(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """``devices/list`` projects the persisted ``offline_since`` onto each device."""
    controller = make_controller(tmp_path)
    controller._scanner.devices = [_device("kitchen", state=DeviceState.OFFLINE)]
    controller._metadata_store.set_field("kitchen.yaml", "offline_since", time.time() - 7200)

    response = await controller.list_devices()

    offline_seconds = response.configured[0].runtime_state.offline_seconds
    assert offline_seconds is not None
    assert 7195 < offline_seconds < 7210


async def test_list_devices_leaves_offline_duration_null_without_a_stamp(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """No stamp means no honest duration to report."""
    controller = make_controller(tmp_path)
    controller._scanner.devices = [_device("kitchen", state=DeviceState.OFFLINE)]

    response = await controller.list_devices()

    assert response.configured[0].runtime_state.offline_seconds is None


async def test_going_offline_stamps_offline_since(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Leaving ONLINE anchors the clock the pill counts from."""
    controller = make_controller(tmp_path)
    _seed(controller, _device("kitchen", state=DeviceState.ONLINE))

    controller._on_state_change("kitchen", DeviceState.OFFLINE, "ping")
    await controller.list_devices()

    stamp = controller._metadata_store.get_field("kitchen.yaml", "offline_since")
    assert stamp is not None
    assert abs(time.time() - stamp) < 5


async def test_coming_back_online_clears_offline_since(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A device that is back has no offline duration to show."""
    controller = make_controller(tmp_path)
    _seed(controller, _device("kitchen", state=DeviceState.OFFLINE))
    controller._metadata_store.set_field("kitchen.yaml", "offline_since", time.time() - 60)

    controller._on_state_change("kitchen", DeviceState.ONLINE, "mdns")
    await controller.list_devices()

    assert not controller._metadata_store.get_field("kitchen.yaml", "offline_since")


async def test_startup_does_not_restart_the_offline_clock(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """An UNKNOWN -> OFFLINE settle keeps a stamp from a previous run.

    This is the case the feature exists for: a battery device asleep across
    a dashboard restart must not have its duration reset to zero.
    """
    controller = make_controller(tmp_path)
    _seed(controller, _device("kitchen", state=DeviceState.UNKNOWN))
    earlier = time.time() - 86400
    controller._metadata_store.set_field("kitchen.yaml", "offline_since", earlier)

    controller._on_state_change("kitchen", DeviceState.OFFLINE, "ping")
    await controller.list_devices()

    assert controller._metadata_store.get_field("kitchen.yaml", "offline_since") == earlier


async def test_startup_seeds_a_stamp_when_there_is_none(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A first sighting of an already-offline device anchors from now."""
    controller = make_controller(tmp_path)
    _seed(controller, _device("kitchen", state=DeviceState.UNKNOWN))

    controller._on_state_change("kitchen", DeviceState.OFFLINE, "ping")
    await controller.list_devices()

    stamp = controller._metadata_store.get_field("kitchen.yaml", "offline_since")
    assert stamp is not None
    assert abs(time.time() - stamp) < 5
