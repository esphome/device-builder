"""Shared fixtures for ``tests/controllers/firmware/``.

Most handler-level tests in this package were each carrying their
own ``_controller(tmp_path)`` helper that built a stub
``FirmwareController`` with ``__new__``, wired a real
``DashboardSettings`` for path validation, and stubbed the
queue / persistence / supersede / bus surface. The bodies were
nearly identical across a dozen files; centralising the build
here keeps them in sync when the controller's attribute set
shifts (every refactor that adds a new ``self._something`` had
to chase the same pattern across every test file before this).

Tests instantiate via the ``firmware_controller_factory``
fixture. The factory exposes three independent opt-ins
(``with_settings`` / ``with_queue`` / ``with_terminate``) so
each test file gets exactly the surface its handler-under-test
actually touches — a refactor that accidentally reaches further
into the controller (e.g. a ``get_jobs`` call that suddenly
hits ``_queue``) crashes with ``AttributeError`` instead of
silently absorbing into a stub.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.controllers.config import DashboardSettings
from esphome_device_builder.controllers.firmware import FirmwareController
from esphome_device_builder.helpers.event_bus import Event, EventBus
from esphome_device_builder.models import EventType, FirmwareJob


class EnqueueStep(StrEnum):
    """Step labels in the ``capture_enqueue_order`` log.

    Same shape as ``StreamEvent`` (PR #212): a small enum keeps
    callers from drifting on bare strings — a typo in either the
    helper or the assertion would otherwise pass silently
    (``log[0] == ("putt", job)`` is a valid tuple comparison that
    never matches).
    """

    PUT = "put"
    FIRE = "fire"


class FirmwareControllerFactory(Protocol):
    """
    Type for the ``firmware_controller_factory`` fixture.

    Exported so test files can annotate their fixture parameter
    without each redeclaring the callable shape — pylance / mypy
    then know that ``factory(...)`` returns a
    ``FirmwareController`` and that the kit flags are
    keyword-only.
    """

    def __call__(
        self,
        *jobs: FirmwareJob,
        with_settings: bool = ...,
        with_queue: bool = ...,
        with_terminate: bool = ...,
        with_real_persistence: bool = ...,
        with_real_bus: bool = ...,
    ) -> FirmwareController: ...


@pytest.fixture
def firmware_controller_factory(
    tmp_path: Path,
) -> FirmwareControllerFactory:
    """
    Build stub ``FirmwareController`` instances wired to ``tmp_path``.

    Returns a callable: ``factory(*jobs, with_settings=True,
    with_queue=False, with_terminate=False)``.

    Three kit flags compose, each adding only the attributes the
    relevant code path reads — keeps the test surface honest
    about what each test exercises:

    - ``with_settings=True`` (default): wire ``self._db.settings``
      to a ``DashboardSettings`` whose ``config_dir`` is
      ``tmp_path``. Needed by every handler that calls
      ``rel_path``. Pass ``False`` for in-memory job inspectors
      where reading ``settings`` should hard-fail rather than
      silently use a stub.

    - ``with_queue=False`` (default): when set ``True``, install
      an ``AsyncMock`` stub for ``_queue``. The submission handlers
      (``compile`` / ``upload`` / ``install`` / ``clean`` /
      ``rename`` / ``compile_bulk`` / ``install_bulk`` /
      ``reset_build_env``) need this kit. The validator-only tests
      (``test_traversal_validation`` / ``test_get_binaries`` /
      ``test_download``) do not — leaving ``_queue``
      unattributed makes a regression that suddenly tries to
      enqueue a rejected request crash visibly.

    - ``with_terminate=False`` (default): when set ``True``,
      install ``_current_job`` / ``_current_process`` /
      ``_cancel_requested`` / ``_terminate_current_process``.
      Only ``cancel`` reaches into these.

    - ``with_real_persistence=False`` (default): ``_persist_jobs``
      is replaced with an ``AsyncMock`` so handler-wiring tests
      can ``assert_awaited_once()`` / ``assert_not_awaited()``
      without writing to disk. End-to-end persistence tests
      (``test_persistence.py``) pass ``True`` to leave the real
      method bound — that exercises ``metadata_transaction``
      against ``tmp_path/.device-builder.json`` and survives
      implementation rewrites of the on-disk shape.

    - ``with_real_bus=False`` (default): ``_db.bus`` is a
      ``MagicMock`` — fine for tests that ignore the bus or use
      ``capture_firmware_events`` to replace it. Pass ``True`` for
      tests that drive the bus directly (subscribe a real listener
      to observe streamed events, fire events from another task)
      so the existing ``EventBus`` semantics — synchronous
      delivery, dedupe by listener identity — match production.
      Replaces the per-test ``_make_controller`` helpers in
      ``test_follow_job_race.py`` / ``test_follow_jobs_race.py``.

    Always present: ``_jobs`` (populated from positional
    arguments), ``_db.bus`` (``MagicMock`` or ``EventBus`` per
    ``with_real_bus``), and ``_db.create_background_task`` (no-op
    stub so ``start()`` can compose against it without spawning a
    runner).
    """

    def _make(
        *jobs: FirmwareJob,
        with_settings: bool = True,
        with_queue: bool = False,
        with_terminate: bool = False,
        with_real_persistence: bool = False,
        with_real_bus: bool = False,
    ) -> FirmwareController:
        controller = FirmwareController.__new__(FirmwareController)
        controller._jobs = {j.job_id: j for j in jobs}
        if not with_real_persistence:
            controller._persist_jobs = AsyncMock()

        bus: EventBus | MagicMock = EventBus() if with_real_bus else MagicMock()
        # ``remote_build`` defaults to ``None`` so the firmware
        # controller's ``_resolve_install_source`` helper sees the
        # same shape it does in production before
        # ``DeviceBuilder.start()`` has constructed the
        # ``RemoteBuildController`` — without the seed an attribute
        # lookup would raise ``AttributeError`` and mask the
        # silent-fallback-LOCAL semantic.
        db_attrs: dict[str, Any] = {"bus": bus, "remote_build": None}
        if with_settings:
            settings = DashboardSettings()
            settings.config_dir = tmp_path
            settings.absolute_config_dir = tmp_path.resolve()
            db_attrs["settings"] = settings
        controller._db = type("DB", (), db_attrs)()
        # ``start()`` schedules the queue runner via
        # ``self._db.create_background_task``; persistence tests that drive
        # through ``start()`` need a no-op so the runner doesn't actually
        # spawn. Attach to the instance (not the class) so descriptor
        # binding doesn't treat the lambda as an unbound method.
        controller._db.create_background_task = lambda coro: coro.close()

        if with_queue:
            controller._queue = AsyncMock()

        if with_terminate:
            controller._current_job = None
            controller._current_process = None
            controller._cancel_requested = set()
            controller._cancel_events = {}
            controller._terminate_current_process = AsyncMock()

        return controller

    return _make


CaptureEventsFactory = Callable[..., list[Event]]
CaptureEnqueueOrderFactory = Callable[..., list[tuple[EnqueueStep, Any]]]


@pytest.fixture
def capture_firmware_events() -> Iterator[CaptureEventsFactory]:
    """Yield a factory that swaps a controller's bus for a real ``EventBus``.

    Same shape as the previous function-style helper — tests call
    ``capture_firmware_events(controller, EventType.X, ...)`` and
    get a live ``list[Event]``. The fixture wrapper tracks every
    swap and restores ``controller._db.bus`` to its original value
    on teardown so a test that holds a controller reference past
    the assertion sees the original bus, not a stale fake.

    Tests pull the fixture in by adding ``capture_firmware_events``
    to their signature; no ``with`` block needed.
    """
    swaps: list[tuple[FirmwareController, Any]] = []

    def _factory(
        controller: FirmwareController,
        *event_types: EventType,
    ) -> list[Event]:
        bus = EventBus()
        captured: list[Event] = []
        for event_type in event_types:
            bus.add_listener(event_type, captured.append)
        swaps.append((controller, controller._db.bus))
        controller._db.bus = bus
        return captured

    yield _factory

    for controller, original_bus in swaps:
        controller._db.bus = original_bus


@pytest.fixture
def capture_enqueue_order() -> Iterator[CaptureEnqueueOrderFactory]:
    """Yield a factory that traces ``_queue.put`` + ``bus.fire`` into one ordered log.

    Each ``await self._queue.put(job)`` appends ``(EnqueueStep.PUT, job)``
    and each broadcast for a subscribed ``EventType`` appends
    ``(EnqueueStep.FIRE, Event)``. Tests assert the put-then-fire
    ordering by index in the returned list — the previous shape
    spread the same contract across a parent ``MagicMock`` whose
    ``method_calls`` log was walked with two ``.index()`` calls and
    a ``parent.bus.fire.assert_any_call(...)`` follow-up.

    The internal queue is a real ``asyncio.Queue`` so a runner can
    still dequeue if the test exercises that path. Auto-restore on
    teardown reinstates the original ``_queue`` and ``_db.bus`` so
    sibling tests in the same xdist worker don't see leaked stubs.
    """
    swaps: list[tuple[FirmwareController, Any, Any]] = []

    def _factory(
        controller: FirmwareController,
        *event_types: EventType,
    ) -> list[tuple[EnqueueStep, Any]]:
        log: list[tuple[EnqueueStep, Any]] = []
        inner_queue: asyncio.Queue[FirmwareJob] = asyncio.Queue()

        async def _trace_put(item: FirmwareJob) -> None:
            log.append((EnqueueStep.PUT, item))
            await inner_queue.put(item)

        queue_proxy = MagicMock()
        queue_proxy.put = _trace_put
        queue_proxy.get = inner_queue.get
        queue_proxy.qsize = inner_queue.qsize

        bus = EventBus()
        for event_type in event_types:
            bus.add_listener(event_type, lambda event: log.append((EnqueueStep.FIRE, event)))

        swaps.append((controller, controller._queue, controller._db.bus))
        controller._queue = queue_proxy
        controller._db.bus = bus
        return log

    yield _factory

    for controller, original_queue, original_bus in swaps:
        controller._queue = original_queue
        controller._db.bus = original_bus
