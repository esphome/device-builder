"""End-to-end smoke test for ``DeviceBuilder`` start / stop.

Catches controller-wiring regressions that the per-controller
test files miss: a new controller added to ``__init__`` but not
to ``start()``'s init list, or omitted from the
``collect_api_commands`` loop, would silently lose its api
commands without breaking any narrower test.

These tests instantiate a real ``DeviceBuilder`` against a
``tmp_path`` config dir and run ``start`` + ``stop``. The three
network-touching surfaces (firmware's ``_verify_esphome_importable``
subprocess, devices' state-monitor zeroconf browser, MQTT
coordinator's broker connect) are patched out so the test stays
hermetic; everything else runs end-to-end including catalog
loads and the command-handler registration walk.
"""

from __future__ import annotations

import asyncio

import pytest

from esphome_device_builder.device_builder import DeviceBuilder

from .conftest import MakeSettingsFactory

# ---------------------------------------------------------------------------
# Pre-start state
# ---------------------------------------------------------------------------


def test_init_leaves_controllers_unset(make_settings: MakeSettingsFactory) -> None:
    """``DeviceBuilder()`` instantiates with controllers ``None`` until ``start()``.

    Pin the documented "controllers populated in start()" contract
    so a refactor that eagerly constructs them in ``__init__``
    surfaces here — eager construction would mean a missed
    ``settings`` dependency wouldn't crash until first command
    instead of at construction time, which is a worse failure mode.
    """
    db = DeviceBuilder(make_settings(with_core_path=True))

    assert db.auth is None
    assert db.boards is None
    assert db.components is None
    assert db.config is None
    assert db.devices is None
    assert db.automations is None
    assert db.firmware is None
    assert db.editor is None
    assert db.command_handlers == {}
    assert db.loop is None
    # Pool eagerly constructed so probes pre-start see it.
    assert db._executor is not None


# ---------------------------------------------------------------------------
# start()
# ---------------------------------------------------------------------------


async def test_start_initialises_all_controllers(
    make_settings: MakeSettingsFactory, _hermetic_lifecycle: None
) -> None:
    """``start()`` populates every controller attr.

    A new controller added to ``__init__`` (the documented
    ``None``-defaults) but missed in ``start()`` would silently
    leave that attr as ``None`` — and any code path that reaches
    for it later crashes with ``AttributeError`` on a
    ``NoneType``. Pin the full set so the regression class can't
    hide.
    """
    db = DeviceBuilder(make_settings(with_core_path=True))
    try:
        await db.start()

        assert db.loop is not None
        assert db.auth is not None
        assert db.boards is not None
        assert db.components is not None
        assert db.config is not None
        assert db.devices is not None
        assert db.automations is not None
        assert db.firmware is not None
        assert db.editor is not None
    finally:
        await db.stop()


async def test_start_registers_command_handlers_from_every_controller(
    make_settings: MakeSettingsFactory, _hermetic_lifecycle: None
) -> None:
    """``command_handlers`` includes one entry per controller's @api_command set.

    Pin a sample of well-known commands rather than the full
    inventory (which churns with every feature add) — enough to
    catch a refactor that dropped a controller from the
    ``collect_api_commands`` loop.
    """
    db = DeviceBuilder(make_settings(with_core_path=True))
    try:
        await db.start()

        # Sample one command per controller. If the loop ever skipped
        # a controller these would fail.
        assert "auth/login" in db.command_handlers  # AuthController
        assert "boards/get_boards" in db.command_handlers  # BoardCatalog
        assert "components/get_components" in db.command_handlers  # ComponentCatalog
        assert "config/version" in db.command_handlers  # ConfigController
        assert "devices/list" in db.command_handlers  # DevicesController
        assert "firmware/compile" in db.command_handlers  # FirmwareController
        assert "editor/validate_yaml" in db.command_handlers  # EditorController

        # Built-in commands wired post-loop.
        assert "ping" in db.command_handlers
        assert "subscribe_events" in db.command_handlers
        # ``auth`` is an alias of ``auth/login`` so both forms work on the wire.
        assert db.command_handlers["auth"] is db.command_handlers["auth/login"]
    finally:
        await db.stop()


async def test_start_spawns_background_polling_task(
    make_settings: MakeSettingsFactory, _hermetic_lifecycle: None
) -> None:
    """``_bg_task`` is created and live after ``start``.

    ``DeviceBuilder.poll`` calls into ``DevicesController.poll``
    every ``_BG_POLL_INTERVAL_SECONDS`` to pick up file-watcher
    misses; if ``start()`` ever forgot to spawn the task the
    dashboard would silently stop seeing scan changes.
    """
    db = DeviceBuilder(make_settings(with_core_path=True))
    try:
        await db.start()

        assert db._bg_task is not None
        assert not db._bg_task.done()
    finally:
        await db.stop()


async def test_background_poll_skips_when_no_subscribers(
    make_settings: MakeSettingsFactory, _hermetic_lifecycle: None
) -> None:
    """The background loop parks at ``wait_for_subscriber`` until a client connects.

    Polling for filesystem drift is cheap-but-not-free
    (``DeviceScanner._build_cache_keys`` walks the config dir and
    stats every YAML on every tick) and the only consumer of the
    refreshed state is the dashboard UI. With no WS subscriber
    attached, no UI is showing the device list, so the work is
    pure idle CPU. Pin that the gate keeps ``DevicesController.poll``
    asleep across the steady-state interval so a regression that
    drops the gate (or wires the wrong presence object) surfaces
    here instead of in a customer's flame graph.
    """
    db = DeviceBuilder(make_settings(with_core_path=True))
    try:
        await db.start()
        # ``DevicesController.poll`` is what the loop calls under the
        # gate. Wrap it to fire an Event so the test can ``await``
        # the wakeup deterministically instead of busy-looping over
        # ``asyncio.sleep(0)`` and hoping enough turns drain.
        polled = asyncio.Event()
        poll_calls = 0

        async def _counting_poll() -> None:
            nonlocal poll_calls
            poll_calls += 1
            polled.set()

        db.devices.poll = _counting_poll  # type: ignore[method-assign]

        # No subscribers attached: ``polled`` should NOT fire.
        # ``wait_for`` with a tiny timeout is the deterministic
        # negative-case shape — if the gate were broken and the
        # poll fired, the event would set, ``wait_for`` would
        # return, and the assertion below would catch it.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(polled.wait(), timeout=0.05)
        assert poll_calls == 0, (
            f"poll fired {poll_calls} times with no subscribers; gate isn't holding"
        )

        # 0→1 transition wakes the loop. Same ``subscriber()`` ctx
        # manager the WS handler uses — entering opens the gate and
        # ``wait_for_subscriber`` returns, letting the loop call
        # ``poll`` exactly once before parking on the interval wait.
        with db.subscriber_presence.subscriber():
            await asyncio.wait_for(polled.wait(), timeout=1.0)
        assert poll_calls >= 1
    finally:
        await db.stop()


# ---------------------------------------------------------------------------
# stop()
# ---------------------------------------------------------------------------


async def test_stop_cancels_background_tasks_and_tears_down_controllers(
    make_settings: MakeSettingsFactory, _hermetic_lifecycle: None
) -> None:
    """``stop()`` cancels the background task, drains tracked tasks, stops controllers.

    Pin every step in the teardown chain. A graceful shutdown
    that left the bg task spinning would prevent the process from
    exiting on SIGINT — exactly the class of bug a smoke test
    catches that a per-method test doesn't.
    """
    db = DeviceBuilder(make_settings(with_core_path=True))
    await db.start()
    bg_task = db._bg_task
    assert bg_task is not None

    # Add a tracked task so we can verify the drain branch fires.
    drained = asyncio.Event()

    async def _tracked() -> None:
        try:
            await drained.wait()
        except asyncio.CancelledError:
            drained.set()
            raise

    db.create_background_task(_tracked())

    await db.stop()

    # Bg task cancelled.
    assert bg_task.done()
    # Tracked task observed cancellation.
    assert drained.is_set()
    # ``DevicesController.stop()`` clears its ``_unsub_job_completed``
    # back to ``None``, which is the externally-observable signal
    # that the controller-stop branch ran. ``EditorController.stop()``
    # has its own teardown path but no equivalent attr to inspect.
    assert db.devices is not None
    assert db.devices._unsub_job_completed is None  # type: ignore[attr-defined]
    # Executor pool drained.
    assert db._executor is None


async def test_stop_is_safe_without_start(make_settings: MakeSettingsFactory) -> None:
    """``stop()`` on a never-started instance doesn't crash.

    Production calls ``stop()`` from the aiohttp ``on_shutdown``
    hook, which fires even when ``start()`` never completed
    (e.g. early exception during catalog load). The ``if … is
    not None`` guards in ``stop()`` are what make that path
    survivable. Pin them.
    """
    db = DeviceBuilder(make_settings(with_core_path=True))

    # Never called start(); controllers are still ``None``.
    await db.stop()

    # Pool still gets drained even on the early-exit path so the
    # process can exit cleanly.
    assert db._executor is None
