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

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

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
def _capture_monitor_and_mqtt(controller: DevicesController) -> Iterator[list[str]]:
    """Stub the monitor and coordinator only, leaving the scanner real for refine tests."""
    log: list[str] = []

    async def _state_monitor_start() -> None:
        log.append("state_monitor.start")

    async def _state_monitor_stop() -> None:
        log.append("state_monitor.stop")

    async def _mqtt_reconcile(**_kw: object) -> None:
        log.append("mqtt.reconcile")

    async def _mqtt_stop() -> None:
        log.append("mqtt.stop")

    with (
        patch.multiple(
            controller._state_monitor, start=_state_monitor_start, stop=_state_monitor_stop
        ),
        patch.multiple(controller._mqtt_coordinator, reconcile=_mqtt_reconcile, stop=_mqtt_stop),
    ):
        yield log


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
    with _capture_monitor_and_mqtt(controller) as log:

        async def _scan(shallow: bool = False) -> None:
            log.append("scan_shallow" if shallow else "scan")

        with patch.multiple(controller._scanner, scan=_scan):
            yield log


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
        "esphome_device_builder.controllers.devices.importable.ignored_devices_storage_path",
        lambda: tmp_path / "ignored-devices.json",
    )
    (tmp_path / "ignored-devices.json").write_bytes(
        b'{"ignored_devices": ["already-ignored"]}',
    )

    with _capture_inner_lifecycle(controller) as log:
        await controller.start()

    assert controller.state.esphome_cmd == ["python", "-m", "esphome"]
    assert controller.state.ignored_devices == {"already-ignored"}
    # Fact-of-call AND ordering in one assertion: shallow scan first
    # (the state monitor's first sweep reads ``self._scanner.devices``
    # so a swap would have it iterate over an empty list at
    # cold-start), then state_monitor.start, then mqtt.reconcile.
    assert log == ["scan_shallow", "state_monitor.start", "mqtt.reconcile"]
    # JOB_COMPLETED listener registered via the real ``EventBus``-shaped stub.
    assert db.bus.listeners == [(EventType.JOB_COMPLETED, controller._on_firmware_job_completed)]
    assert controller._unsub_job_completed is not None


async def test_start_pre_scan_loads_complete_before_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_db: MakeDbFactory
) -> None:
    """The gathered store/migration/ignore loads all land before the scanner runs."""
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.controller._find_esphome_cmd",
        lambda: ["python", "-m", "esphome"],
    )
    db = make_db(tmp_path)
    controller = DevicesController(db)
    log: list[str] = []

    def _async_recorder(name: str):
        async def _run(*_a: object, **_kw: object) -> None:
            log.append(name)

        return _run

    monkeypatch.setattr(controller._metadata_store, "async_load", _async_recorder("metadata"))
    monkeypatch.setattr(controller._pending_keys, "async_load", _async_recorder("pending_keys"))
    monkeypatch.setattr(controller, "migrate_board_id_user_set", _async_recorder("migrate"))
    monkeypatch.setattr(controller, "_load_ignored_devices", lambda: log.append("ignored"))
    monkeypatch.setattr(controller._scanner, "scan", _async_recorder("scan"))
    with (
        patch.multiple(controller._state_monitor, start=AsyncMock(), stop=AsyncMock()),
        patch.multiple(controller._mqtt_coordinator, reconcile=AsyncMock(), stop=AsyncMock()),
    ):
        await controller.start()
        await controller.stop()

    assert set(log[:4]) == {"metadata", "pending_keys", "migrate", "ignored"}
    assert log[4] == "scan"


async def test_build_size_worker_starts_live_with_delayed_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_db: MakeDbFactory
) -> None:
    """The worker spawns at ``start()`` with its fleet sweep held on the cold-start delay."""
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.controller._find_esphome_cmd",
        lambda: ["python", "-m", "esphome"],
    )
    db = make_db(tmp_path)
    controller = DevicesController(db)
    with _capture_inner_lifecycle(controller):
        await controller.start()
    await asyncio.sleep(0)

    assert controller._build_size._task is not None
    # The armed sweep task is the behavioral proof the delay is wired.
    assert controller._build_size._sweep_task is not None

    with _capture_inner_lifecycle(controller):
        await controller.stop()

    assert controller._build_size._task is None


async def test_start_refines_shallow_seed_then_reconciles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_db: MakeDbFactory
) -> None:
    """The refine task deep-reloads the seeded fleet and re-runs the MQTT reconcile."""
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.controller._find_esphome_cmd",
        list,
    )
    # ``logger.baud_rate`` is resolved-only: the shallow seed carries
    # ``None``, so the refine's deep reload must change the row and
    # push the update to clients.
    (tmp_path / "kitchen.yaml").write_text(
        "esphome:\n  name: kitchen\nlogger:\n  baud_rate: 9600\n", encoding="utf-8"
    )
    db = make_db(tmp_path)
    controller = DevicesController(db)

    with _capture_monitor_and_mqtt(controller) as log:
        await controller.start()
        assert controller._refine_task is not None
        await controller._refine_task
        # The ADDED scan change nudged the debounced reconcile.
        assert controller._mqtt_reconcile_handle is not None
        await controller.stop()

    assert log.count("mqtt.reconcile") == 1
    # stop() cancelled the pending nudge before it fired.
    assert controller._mqtt_reconcile_handle is None
    fired = [event_type for event_type, _data in db.bus.fired]
    assert EventType.DEVICE_ADDED in fired
    assert EventType.DEVICE_UPDATED in fired
    # The refine burst must never read as an edit: one git commit per
    # device per restart otherwise.
    assert EventType.DEVICE_YAML_UPDATED not in fired


async def test_stop_cancels_refine_mid_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_db: MakeDbFactory
) -> None:
    """A shutdown during the refine drain cancels the task and skips the trailing reconcile."""
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.controller._find_esphome_cmd",
        list,
    )
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    db = make_db(tmp_path)
    controller = DevicesController(db)

    async def _parked_reload(_filename: str) -> bool:
        await asyncio.Event().wait()
        return True

    with _capture_monitor_and_mqtt(controller) as log:
        # Patched before start() so any refine-triggered drain is
        # guaranteed to block in the parked reload.
        monkeypatch.setattr(controller._scanner, "reload", _parked_reload)
        await controller.start()
        task = controller._refine_task
        assert task is not None
        await asyncio.sleep(0)
        await controller.stop()

    assert task.done()
    assert controller._refine_task is None
    assert log.count("mqtt.reconcile") == 1


async def test_stop_stops_build_size_before_scanner(tmp_path: Path, make_db: MakeDbFactory) -> None:
    """Build-size stops first: its drain's ``on_refreshed`` requests a scanner reload."""
    controller = DevicesController(make_db(tmp_path))
    log: list[str] = []

    async def _build_size_stop() -> None:
        log.append("build_size.stop")

    async def _scanner_stop() -> None:
        log.append("scanner.stop")

    with (
        _capture_monitor_and_mqtt(controller),
        patch.multiple(controller._build_size, stop=_build_size_stop),
        patch.multiple(controller._scanner, stop=_scanner_stop),
    ):
        await controller.stop()

    assert log.index("build_size.stop") < log.index("scanner.stop")


# ---------------------------------------------------------------------------
# stop()
# ---------------------------------------------------------------------------


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


async def test_poll_is_noop_after_stop(tmp_path: Path, make_db: MakeDbFactory) -> None:
    """``poll()`` no-ops once stopped, so a shutdown-drain GET can't re-arm torn-down work."""
    db = make_db(tmp_path)
    controller = DevicesController(db)
    controller._stopped = True  # as stop() sets it

    with _capture_inner_lifecycle(controller) as log:
        await controller.poll()

    assert log == []


async def test_mqtt_nudge_debounce_coalesces_and_fires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_db: MakeDbFactory
) -> None:
    """Repeated nudges share one timer, and its firing runs a single reconcile."""
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.controller._MQTT_RECONCILE_DEBOUNCE_SECONDS",
        0.0,
    )
    controller = DevicesController(make_db(tmp_path))

    with _capture_monitor_and_mqtt(controller) as log:
        controller._schedule_mqtt_reconcile()
        first = controller._mqtt_reconcile_handle
        assert first is not None
        controller._schedule_mqtt_reconcile()
        assert controller._mqtt_reconcile_handle is first
        await asyncio.sleep(0.01)
        assert controller._mqtt_reconcile_task is not None
        await controller._mqtt_reconcile_task

    assert log == ["mqtt.reconcile"]


async def test_stop_cancels_pending_mqtt_nudge(tmp_path: Path, make_db: MakeDbFactory) -> None:
    """A nudge still in its debounce window is cancelled by stop(), not fired."""
    controller = DevicesController(make_db(tmp_path))

    with _capture_monitor_and_mqtt(controller) as log:
        controller._schedule_mqtt_reconcile()
        assert controller._mqtt_reconcile_handle is not None
        await controller.stop()

    assert controller._mqtt_reconcile_handle is None
    assert "mqtt.reconcile" not in log
