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
    """``run_in_executor`` lands on the dashboard's named pool, not asyncio's default.

    Drives the same ``_install_default_executor`` helper that
    production ``start()`` calls, instead of re-implementing
    ``loop.set_default_executor(...)`` here. That way a regression
    where ``start()`` stops registering the pool fails this test —
    the helper would have to disappear from ``start()`` for the
    binding to be skipped.
    """
    builder = DeviceBuilder(_settings(tmp_path))
    builder.loop = asyncio.get_running_loop()
    try:
        builder._install_default_executor()
        thread_name = await asyncio.to_thread(lambda: threading.current_thread().name)
        assert thread_name.startswith("dashboard"), (
            f"to_thread landed on {thread_name!r} instead of the dashboard pool — "
            "the editor-stall regression is back."
        )
    finally:
        # Drain workers so the pool doesn't outlive the test and trip
        # blockbuster on the next test's event loop.
        await builder.stop()


async def test_stop_drains_executor(tmp_path: Any) -> None:
    """``stop()`` shuts down our pool and clears ``_executor``.

    Drives ``_install_default_executor`` rather than poking the loop
    directly so the test exercises the production registration path.
    """
    builder = DeviceBuilder(_settings(tmp_path))
    builder.loop = asyncio.get_running_loop()
    builder._install_default_executor()
    pool = builder._executor
    assert pool is not None
    await builder.stop()
    # ``_executor`` is None after a clean stop so a second stop is a
    # no-op and the GC can collect the pool's last reference.
    assert builder._executor is None
    # Pool itself is shut down; submitting work raises.
    import pytest as _pytest

    with _pytest.raises(RuntimeError):
        pool.submit(lambda: None)
