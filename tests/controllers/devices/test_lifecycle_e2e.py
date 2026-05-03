"""End-to-end coverage for ``DevicesController`` lifecycle.

The handler-level tests in ``tests/controllers/devices/`` all
bypass ``__init__`` via ``__new__`` and stub
``_scanner`` / ``_state_monitor`` / ``_mqtt_coordinator``
individually — that lets each test target one method but leaves
the wiring code itself uncovered:

- ``__init__`` — constructs the scanner, state monitor, and
  MQTT coordinator and threads their callbacks back to the
  controller.
- ``start()`` — resolves the esphome cmd, loads ignored
  devices, kicks the scanner, starts the state monitor,
  reconciles MQTT, and registers the JOB_COMPLETED listener.
- ``stop()`` — unsubscribes the bus listener and stops the two
  background monitors.
- ``poll()`` — re-scans and reconciles MQTT.

These tests instantiate a real ``DevicesController`` against a
``tmp_path`` config dir and a thin stub ``DeviceBuilder`` so the
``__init__`` body runs in full. The inner monitors' lifecycle
methods are patched as ``AsyncMock`` so ``start`` / ``stop``
don't try to open a zeroconf browser or connect to MQTT — those
are exercised in their own dedicated tests.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.controllers.devices import DevicesController
from esphome_device_builder.models import EventType

from .conftest import MakeDbFactory

# ---------------------------------------------------------------------------
# __init__
# ---------------------------------------------------------------------------


def test_init_threads_state_monitor_callbacks_to_controller_methods(
    tmp_path: Path, make_db: MakeDbFactory
) -> None:
    """State-monitor callbacks point back at ``self._on_*_change`` methods.

    The state monitor was the locus of the "monitor cache drifts
    out of sync with the device" regression in PR #75 — fixed by
    making the callbacks the source-of-truth path. If a future
    refactor accidentally bypasses one of them, that whole class
    of bug returns.
    """
    db = make_db(tmp_path)
    controller = DevicesController(db)

    # Bound-method equality: ``a is b`` fails on bound methods even
    # for the same underlying function on the same instance, so use
    # ``==`` (which compares ``__self__`` + ``__func__``). Either
    # way it's a refactor-catch — a typo'd callback wire would point
    # at a different method or a stub and break this assertion.
    monitor = controller._state_monitor
    assert monitor._on_state_change == controller._on_state_change  # type: ignore[attr-defined]
    assert monitor._on_ip_change == controller._on_ip_change  # type: ignore[attr-defined]
    assert monitor._on_version_change == controller._on_version_change  # type: ignore[attr-defined]
    assert monitor._on_config_hash_change == controller._on_config_hash_change  # type: ignore[attr-defined]
    assert monitor._on_api_encryption_change == controller._on_api_encryption_change  # type: ignore[attr-defined]
    assert monitor._on_importable_added == controller._on_importable_added  # type: ignore[attr-defined]
    assert monitor._on_importable_removed == controller._on_importable_removed  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# start()
# ---------------------------------------------------------------------------


def _stub_inner_lifecycle(controller: DevicesController) -> None:
    """Replace the real start/stop/scan methods with AsyncMocks.

    ``start()`` and ``stop()`` route through the scanner / state
    monitor / MQTT coordinator. Patching their lifecycle methods
    out keeps these tests focused on *DevicesController*'s
    contract; the inner controllers have their own dedicated test
    files.
    """
    controller._scanner.scan = AsyncMock()  # type: ignore[method-assign]
    controller._state_monitor.start = AsyncMock()  # type: ignore[method-assign]
    controller._state_monitor.stop = AsyncMock()  # type: ignore[method-assign]
    controller._mqtt_coordinator.reconcile = AsyncMock()  # type: ignore[method-assign]
    controller._mqtt_coordinator.stop = AsyncMock()  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_start_runs_full_initialisation_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_db: MakeDbFactory
) -> None:
    """``start()`` resolves esphome cmd, loads ignored, scans, starts monitors, subscribes bus.

    Pin the full chain — every step has its own dedicated regression
    elsewhere, but the *order* and *fact-of-call* live here. A
    refactor that reordered (e.g. ``state_monitor.start`` before
    ``scanner.scan``) could cause cold-start ordering bugs the
    individual tests wouldn't catch.

    Call ordering is asserted via a parent ``MagicMock`` that all
    three inner lifecycle hooks attach to: the production code
    awaits ``scanner.scan`` first, then ``state_monitor.start``,
    then ``mqtt_coordinator.reconcile``. The state monitor reads
    ``self._scanner.devices`` for its first sweep, so swapping
    those two would have it iterate over an empty list at
    cold-start.
    """
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.controller._find_esphome_cmd",
        lambda: ["python", "-m", "esphome"],
    )
    db = make_db(tmp_path)
    controller = DevicesController(db)
    _stub_inner_lifecycle(controller)

    # Attach each AsyncMock to a single parent so ``mock_calls`` on
    # the parent records the relative ordering across the three
    # inner controllers.
    parent = MagicMock()
    parent.attach_mock(controller._scanner.scan, "scan")  # type: ignore[arg-type]
    parent.attach_mock(controller._state_monitor.start, "state_monitor_start")  # type: ignore[arg-type]
    parent.attach_mock(controller._mqtt_coordinator.reconcile, "reconcile")  # type: ignore[arg-type]

    # Seed an ignored-devices file so ``_load_ignored_devices`` has
    # something real to process — otherwise it's silently a no-op
    # and we wouldn't observe the executor-dispatch call shape.
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.controller.ignored_devices_storage_path",
        lambda: tmp_path / "ignored-devices.json",
    )
    (tmp_path / "ignored-devices.json").write_bytes(
        b'{"ignored_devices": ["already-ignored"]}',
    )

    await controller.start()

    assert controller._esphome_cmd == ["python", "-m", "esphome"]
    assert controller.ignored_devices == {"already-ignored"}
    controller._scanner.scan.assert_awaited_once()
    controller._state_monitor.start.assert_awaited_once()
    controller._mqtt_coordinator.reconcile.assert_awaited_once()
    # Pin the relative order: scan → state_monitor.start → reconcile.
    observed_order = [c[0] for c in parent.mock_calls]
    assert observed_order == ["scan", "state_monitor_start", "reconcile"]
    # JOB_COMPLETED listener registered via the real ``EventBus``-shaped stub.
    assert db.bus.listeners == [(EventType.JOB_COMPLETED, controller._on_firmware_job_completed)]
    assert controller._unsub_job_completed is not None


# ---------------------------------------------------------------------------
# stop()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_tears_down_monitors_and_unsubscribes(
    tmp_path: Path, make_db: MakeDbFactory
) -> None:
    """``stop()`` unsubscribes the bus listener and stops both monitors."""
    db = make_db(tmp_path)
    controller = DevicesController(db)
    _stub_inner_lifecycle(controller)
    # Pretend ``start()`` already ran and registered a listener.
    unsub_calls: list[bool] = []

    def _unsub() -> None:
        unsub_calls.append(True)

    controller._unsub_job_completed = _unsub

    await controller.stop()

    assert unsub_calls == [True]
    assert controller._unsub_job_completed is None
    controller._mqtt_coordinator.stop.assert_awaited_once()
    controller._state_monitor.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_is_idempotent_without_started_listener(
    tmp_path: Path, make_db: MakeDbFactory
) -> None:
    """``stop()`` before ``start()`` (or after a previous ``stop()``) doesn't crash.

    Pin the ``if self._unsub_job_completed is not None`` guard —
    a refactor that dropped it would crash the second teardown
    on a process restart that calls stop+start+stop.
    """
    db = make_db(tmp_path)
    controller = DevicesController(db)
    _stub_inner_lifecycle(controller)
    # Never started; ``_unsub_job_completed`` is the ``__init__`` default.
    assert controller._unsub_job_completed is None

    await controller.stop()

    controller._mqtt_coordinator.stop.assert_awaited_once()
    controller._state_monitor.stop.assert_awaited_once()


# ---------------------------------------------------------------------------
# poll()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_rescans_and_reconciles_mqtt(tmp_path: Path, make_db: MakeDbFactory) -> None:
    """``poll()`` runs a fresh scan + MQTT reconcile.

    The dashboard's periodic poll path; pin both calls so a
    refactor that dropped either silently breaks file-change /
    broker-rediscovery detection.
    """
    db = make_db(tmp_path)
    controller = DevicesController(db)
    _stub_inner_lifecycle(controller)

    await controller.poll()

    controller._scanner.scan.assert_awaited_once()
    controller._mqtt_coordinator.reconcile.assert_awaited_once()
