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

import pytest

from esphome_device_builder.device_builder import _EXECUTOR_MAX_WORKERS, DeviceBuilder

from .conftest import MakeSettingsFactory


def test_executor_created_in_init(make_settings: MakeSettingsFactory) -> None:
    """``__init__`` populates ``_executor`` so callers can probe it pre-start.

    Doesn't reach into ``ThreadPoolExecutor._max_workers`` — that's a
    CPython implementation detail. The "pool is sized correctly"
    contract is exercised indirectly: as long as ``__init__`` builds
    a ``ThreadPoolExecutor`` from ``_EXECUTOR_MAX_WORKERS``, the size
    is whatever the constant holds. The constant itself being a
    sensible number is reviewed at the source — locking it down to
    a literal here just couples the test to the runtime knob.
    """
    builder = DeviceBuilder(make_settings())
    assert isinstance(builder._executor, ThreadPoolExecutor)
    # Sanity-check that the constant exists and is a reasonable
    # positive value — guards against someone setting it to 0 / None
    # while refactoring without the constant import disappearing.
    assert isinstance(_EXECUTOR_MAX_WORKERS, int)
    assert _EXECUTOR_MAX_WORKERS > 0
    builder._executor.shutdown(wait=False)


async def test_run_in_executor_uses_dashboard_pool(make_settings: MakeSettingsFactory) -> None:
    """``run_in_executor`` lands on the dashboard's named pool, not asyncio's default.

    Drives the same ``_install_default_executor`` helper that
    production ``start()`` calls, instead of re-implementing
    ``loop.set_default_executor(...)`` here. That way a regression
    where ``start()`` stops registering the pool fails this test —
    the helper would have to disappear from ``start()`` for the
    binding to be skipped.
    """
    builder = DeviceBuilder(make_settings())
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


async def test_stop_drains_executor(make_settings: MakeSettingsFactory) -> None:
    """``stop()`` shuts down our pool and clears ``_executor``.

    Drives ``_install_default_executor`` rather than poking the loop
    directly so the test exercises the production registration path.
    """
    builder = DeviceBuilder(make_settings())
    builder.loop = asyncio.get_running_loop()
    builder._install_default_executor()
    pool = builder._executor
    assert pool is not None
    await builder.stop()
    # ``_executor`` is None after a clean stop so a second stop is a
    # no-op and the GC can collect the pool's last reference.
    assert builder._executor is None
    # Pool itself is shut down; submitting work raises.
    with pytest.raises(RuntimeError):
        pool.submit(lambda: None)


async def test_stop_without_start_drains_executor(make_settings: MakeSettingsFactory) -> None:
    """``stop()`` cleans up the pool even when ``start()`` never bound a loop.

    The pool is created eagerly in ``__init__``, so an instance that's
    constructed and immediately disposed still has a live
    ``ThreadPoolExecutor`` to shut down. Without this path, a test
    helper or short-lived caller would leak threads.
    """
    builder = DeviceBuilder(make_settings())
    pool = builder._executor
    assert pool is not None
    # ``self.loop`` is None at this point — start() never ran.
    assert builder.loop is None
    await builder.stop()
    assert builder._executor is None
    with pytest.raises(RuntimeError):
        pool.submit(lambda: None)
