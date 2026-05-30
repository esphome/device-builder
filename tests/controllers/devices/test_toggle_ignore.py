"""End-to-end coverage for ``DevicesController.toggle_ignore``.

The handler manages the ``ignored_devices`` set (used by the
import-list filter), persists the change to disk via the
executor-routed ``_save_ignored_devices``, and — when an
``AdoptableDevice`` is currently cached for that name — mirrors
the new flag onto the cache + re-publishes ``IMPORTABLE_DEVICE_ADDED``
so subscribed frontends update the badge without waiting for the
next discovery cycle.

Four contracts pinned:

1. ``ignore=True`` adds the name to the set; ``ignore=False`` removes it.
2. The persist path runs in the executor (file actually lands on disk).
3. A cached ``AdoptableDevice`` gets its ``ignored`` flag mirrored,
   and an ``IMPORTABLE_DEVICE_ADDED`` event fires with the updated
   model so the frontend re-renders the badge.
4. The event-fire branch is gated on a meaningful state change —
   re-asserting the same ``ignored`` value doesn't fire a duplicate
   event.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from esphome_device_builder.controllers.devices import DevicesController
from esphome_device_builder.helpers.event_bus import Event
from esphome_device_builder.models import AdoptableDevice, EventType

from .conftest import CaptureDevicesEventsFactory, MakeControllerFactory


def _seed_for_toggle(
    controller: DevicesController,
    tmp_path: Path,
    capture_devices_events: CaptureDevicesEventsFactory,
) -> tuple[list[Event], Path]:
    """Wire ``import_result`` + the events capture for the toggle path.

    Returns ``(events, ignored_path)`` so the test can assert
    against fired events and the on-disk state of the ignored
    list. ``_save_ignored_devices`` walks ``ignored_devices_storage_path()``
    which the production loader keys off ``CORE.config_path``;
    monkeypatching it onto a known location under ``tmp_path``
    is what lets the test inspect what landed on disk without
    spinning up a full ``DashboardSettings``.

    The events list is the live capture from
    ``capture_devices_events`` — only ``IMPORTABLE_DEVICE_ADDED``
    is subscribed since that's the toggle path's only broadcast.
    """
    fired = capture_devices_events(controller, EventType.IMPORTABLE_DEVICE_ADDED)
    controller.state.import_result = {}
    controller.state.ignored_devices = set()
    return fired, tmp_path / "ignored-devices.json"


@pytest.fixture
def _patch_ignored_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redirect ``ignored_devices_storage_path`` at ``tmp_path/ignored-devices.json``.

    Production resolves the path from ``CORE.config_path`` which
    isn't set in the test process. Pinning the redirect makes
    ``_save_ignored_devices`` write to a known location the test
    can read back.
    """
    target = tmp_path / "ignored-devices.json"
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.importable.ignored_devices_storage_path",
        lambda: target,
    )


async def test_toggle_ignore_true_adds_to_set_and_persists(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    _patch_ignored_path: None,
    capture_devices_events: CaptureDevicesEventsFactory,
) -> None:
    """``ignore=True`` adds the name and writes the updated list to disk."""
    controller = make_controller(tmp_path)
    _fired, ignored_path = _seed_for_toggle(controller, tmp_path, capture_devices_events)

    await controller.toggle_ignore(name="kitchen-1a2b3c")

    assert "kitchen-1a2b3c" in controller.state.ignored_devices
    # Persist landed on disk via the executor.
    assert ignored_path.exists()
    payload = json.loads(ignored_path.read_text("utf-8"))
    assert payload == {"ignored_devices": ["kitchen-1a2b3c"]}


async def test_toggle_ignore_false_removes_and_persists(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    _patch_ignored_path: None,
    capture_devices_events: CaptureDevicesEventsFactory,
) -> None:
    """``ignore=False`` discards the name and writes the trimmed list.

    Pin ``set.discard`` (not ``remove``) — discard is silent on
    unknown names, which the toggle UI relies on so a duplicate
    "show in list" click on a never-ignored device doesn't blow
    up.
    """
    controller = make_controller(tmp_path)
    _fired, ignored_path = _seed_for_toggle(controller, tmp_path, capture_devices_events)
    controller.state.ignored_devices.add("kitchen-1a2b3c")

    await controller.toggle_ignore(name="kitchen-1a2b3c", ignore=False)

    assert "kitchen-1a2b3c" not in controller.state.ignored_devices
    payload = json.loads(ignored_path.read_text("utf-8"))
    assert payload == {"ignored_devices": []}

    # Discarding a name that isn't there is a no-op (no exception).
    await controller.toggle_ignore(name="never-ignored", ignore=False)


async def test_toggle_ignore_mirrors_flag_onto_cached_adoptable_and_fires(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    _patch_ignored_path: None,
    capture_devices_events: CaptureDevicesEventsFactory,
) -> None:
    """When an ``AdoptableDevice`` is cached, its ``ignored`` flag is mirrored.

    The frontend's import list reads ``AdoptableDevice.ignored``
    to render the badge; without re-publishing the model the
    badge would stay stale until the next mDNS re-discovery
    cycle. Pin both the cache mutation and the
    ``IMPORTABLE_DEVICE_ADDED`` re-fire.
    """
    controller = make_controller(tmp_path)
    fired, _ignored_path = _seed_for_toggle(controller, tmp_path, capture_devices_events)
    controller.state.import_result["kitchen-1a2b3c"] = AdoptableDevice(
        name="kitchen-1a2b3c",
        friendly_name="Kitchen",
        package_import_url="github://acme/firmware.yaml",
        project_name="acme.kitchen",
        project_version="1.0.0",
        network="wifi",
        ignored=False,
    )

    await controller.toggle_ignore(name="kitchen-1a2b3c", ignore=True)

    cached = controller.state.import_result["kitchen-1a2b3c"]
    assert cached.ignored is True
    # Other identity fields survive — only ``ignored`` was flipped.
    assert cached.name == "kitchen-1a2b3c"
    assert cached.network == "wifi"

    # Exactly one IMPORTABLE_DEVICE_ADDED event fired with the updated model.
    assert len(fired) == 1
    assert fired[0].event_type == EventType.IMPORTABLE_DEVICE_ADDED
    assert fired[0].data == {"device": cached}


async def test_toggle_ignore_does_not_fire_when_state_unchanged(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    _patch_ignored_path: None,
    capture_devices_events: CaptureDevicesEventsFactory,
) -> None:
    """Re-asserting an already-set flag doesn't fire a duplicate event.

    Without the ``existing.ignored != ignore`` guard, every
    repeat call would re-publish the same ``IMPORTABLE_DEVICE_ADDED``
    payload — a debugging nightmare for anyone watching the bus
    and a wasted round-trip for every connected frontend.
    """
    controller = make_controller(tmp_path)
    fired, _ignored_path = _seed_for_toggle(controller, tmp_path, capture_devices_events)
    controller.state.import_result["kitchen-1a2b3c"] = AdoptableDevice(
        name="kitchen-1a2b3c",
        friendly_name="Kitchen",
        package_import_url="github://acme/firmware.yaml",
        project_name="acme.kitchen",
        project_version="1.0.0",
        network="wifi",
        ignored=True,  # already ignored
    )
    controller.state.ignored_devices.add("kitchen-1a2b3c")

    # Re-asserting the same value.
    await controller.toggle_ignore(name="kitchen-1a2b3c", ignore=True)

    # Cache untouched.
    assert controller.state.import_result["kitchen-1a2b3c"].ignored is True
    # No event fired — the state didn't change.
    assert fired == []
