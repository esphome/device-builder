"""Default-executor configuration is wired up by ``DeviceBuilder``.

The whole point of bumping the executor pool size is keeping
foreground work (devices/list, editor open) responsive when the
ping-sweep DNS resolves saturate threads. If this is silently
removed or moved past ``start()``, ``loop.run_in_executor`` falls
back to asyncio's default-default — ``min(32, cpu+4)`` threads —
and the editor stall regression returns. Lock that down with a
test that asserts the named pool is actually the loop's default.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from esphome_device_builder.controllers.config import DashboardSettings
from esphome_device_builder.device_builder import _EXECUTOR_MAX_WORKERS, DeviceBuilder


def _settings(tmp_path: Any) -> DashboardSettings:
    settings = DashboardSettings()
    settings.config_dir = tmp_path
    settings.absolute_config_dir = tmp_path.resolve()
    return settings


def test_executor_created_in_init(tmp_path: Any) -> None:
    """``__init__`` populates ``_executor`` so callers can probe it pre-start."""
    builder = DeviceBuilder(_settings(tmp_path))
    assert isinstance(builder._executor, ThreadPoolExecutor)
    # Pin to the module-level constant — the value isn't load-bearing
    # on its own, but the assertion ensures we actually picked a
    # value (not the asyncio default) and ties the test to whatever
    # number ``_EXECUTOR_MAX_WORKERS`` is set to today.
    assert builder._executor._max_workers == _EXECUTOR_MAX_WORKERS
    builder._executor.shutdown(wait=False)


async def test_run_in_executor_uses_dashboard_pool(tmp_path: Any) -> None:
    """``run_in_executor`` should land on the dashboard's named pool, not asyncio's default.

    ``DeviceBuilder.start()`` does a lot more than register the
    executor (it spins up controllers, opens mDNS, etc.) — too much
    to set up in a unit test. Instead, mirror the one bit of
    ``start`` we care about: call ``set_default_executor`` with the
    builder's own pool, then assert the running thread name carries
    the prefix.
    """
    builder = DeviceBuilder(_settings(tmp_path))
    loop = asyncio.get_running_loop()
    assert builder._executor is not None  # type narrowing
    loop.set_default_executor(builder._executor)
    try:
        thread_name = await asyncio.to_thread(lambda: threading.current_thread().name)
        assert thread_name.startswith("dashboard"), (
            f"to_thread landed on {thread_name!r} instead of the dashboard pool — "
            "the editor-stall regression is back."
        )
    finally:
        # Drain workers so the pool doesn't outlive the test and trip
        # blockbuster on the next test's event loop.
        await loop.shutdown_default_executor()


async def test_stop_drains_executor(tmp_path: Any) -> None:
    """``stop()`` clears ``_executor`` after shutting it down."""
    builder = DeviceBuilder(_settings(tmp_path))
    builder.loop = asyncio.get_running_loop()
    assert builder._executor is not None
    builder.loop.set_default_executor(builder._executor)
    await builder.stop()
    # ``_executor`` is None after a clean stop so a second stop is a
    # no-op and the GC can collect the pool's last reference.
    assert builder._executor is None
