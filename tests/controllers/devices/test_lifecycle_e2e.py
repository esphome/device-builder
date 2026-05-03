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

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

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


@contextmanager
def _capture_inner_lifecycle(controller: DevicesController) -> Iterator[list[str]]:
    """Patch the real start/stop/scan methods with stubs that record into a flat log.

    ``start()`` and ``stop()`` route through the scanner / state
    monitor / MQTT coordinator. Patching their lifecycle methods
    out keeps these tests focused on *DevicesController*'s
    contract; the inner controllers have their own dedicated test
    files.

    Context-manager shape so the patches restore on exit (success
    *or* failure). Each test in this module builds its own fresh
    ``DevicesController``, so there are no shared instances to leak
    onto — the auto-restore is for *intra-test* hygiene: the
    captured stubs only intercept calls inside the ``with`` block,
    which makes the scope of the capture explicit at the call site.

    Each stub appends a single label string to the yielded list so
    tests assert on the call sequence in one comparison instead of
    scattering ``MagicMock.assert_awaited_once`` lines and a parent
    ``attach_mock`` ordering plumbing — same shape as
    ``capture_enqueue_order`` for the firmware queue/bus pair.
    """
    log: list[str] = []

    async def _scan() -> None:
        log.append("scan")

    async def _state_monitor_start() -> None:
        log.append("state_monitor.start")

    async def _state_monitor_stop() -> None:
        log.append("state_monitor.stop")

    async def _mqtt_reconcile() -> None:
        log.append("mqtt.reconcile")

    async def _mqtt_stop() -> None:
        log.append("mqtt.stop")

    with (
        patch.multiple(controller._scanner, scan=_scan),
        patch.multiple(
            controller._state_monitor, start=_state_monitor_start, stop=_state_monitor_stop
        ),
        patch.multiple(controller._mqtt_coordinator, reconcile=_mqtt_reconcile, stop=_mqtt_stop),
    ):
        yield log


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

    with _capture_inner_lifecycle(controller) as log:
        await controller.start()

    assert controller._esphome_cmd == ["python", "-m", "esphome"]
    assert controller.ignored_devices == {"already-ignored"}
    # Fact-of-call AND ordering in one assertion: scan first (the
    # state monitor's first sweep reads ``self._scanner.devices``
    # so a swap would have it iterate over an empty list at
    # cold-start), then state_monitor.start, then mqtt.reconcile.
    assert log == ["scan", "state_monitor.start", "mqtt.reconcile"]
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
    # Pretend ``start()`` already ran and registered a listener.
    unsub_calls: list[bool] = []

    def _unsub() -> None:
        unsub_calls.append(True)

    controller._unsub_job_completed = _unsub

    with _capture_inner_lifecycle(controller) as log:
        await controller.stop()

    assert unsub_calls == [True]
    assert controller._unsub_job_completed is None
    assert log == ["mqtt.stop", "state_monitor.stop"]


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
    # Never started; ``_unsub_job_completed`` is the ``__init__`` default.
    assert controller._unsub_job_completed is None

    with _capture_inner_lifecycle(controller) as log:
        await controller.stop()

    assert log == ["mqtt.stop", "state_monitor.stop"]


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

    with _capture_inner_lifecycle(controller) as log:
        await controller.poll()

    assert log == ["scan", "mqtt.reconcile"]
