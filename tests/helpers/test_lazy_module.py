"""Coverage for ``helpers.lazy_module.async_import_module``.

Exercises the four branches of the import dispatcher: cache hit,
concurrent-call future dedup, ``sys.modules`` short-circuit, and
the first-time executor import path (success + failure).
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from esphome_device_builder.helpers import lazy_module


@pytest.fixture(autouse=True)
def _reset_lazy_module_state() -> None:
    """Wipe the per-module cache/future maps between tests."""
    lazy_module._cache.clear()
    lazy_module._futures.clear()


def _make_fake_module(name: str) -> types.ModuleType:
    """Return a fresh module object and register it under *name*."""
    module = types.ModuleType(name)
    module.marker = object()  # type: ignore[attr-defined]
    sys.modules[name] = module
    return module


async def test_async_import_module_first_call_imports_via_executor() -> None:
    """A first-time call runs the importlib hop and caches the module."""
    # ``http.cookiejar`` is in stdlib but rarely preloaded; if pytest already
    # has it, pop and re-import so the executor branch actually runs.
    name = "http.cookiejar"
    sys.modules.pop(name, None)

    result = await lazy_module.async_import_module(name)
    assert result is sys.modules[name]
    assert lazy_module._cache[name] is result
    assert hasattr(result, "CookieJar")  # sanity: real module, not a stub
    assert name not in lazy_module._futures


async def test_async_import_module_returns_cached_on_repeat_call() -> None:
    """Once cached, repeat callers skip the executor entirely."""
    name = "esphome_device_builder._test_lazy_cached"
    fake = types.ModuleType(name)
    lazy_module._cache[name] = fake

    result = await lazy_module.async_import_module(name)
    assert result is fake


async def test_async_import_module_short_circuits_on_sys_modules() -> None:
    """A module already in ``sys.modules`` is hoisted into the cache without re-import."""
    name = "esphome_device_builder._test_lazy_sysmodules"
    fake = _make_fake_module(name)

    try:
        result = await lazy_module.async_import_module(name)
        assert result is fake
        assert lazy_module._cache[name] is fake
    finally:
        sys.modules.pop(name, None)


async def test_async_import_module_dedupes_concurrent_callers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second caller arriving while the first is in-flight awaits the same future."""
    name = "esphome_device_builder._test_lazy_concurrent"
    fake = types.ModuleType(name)
    gate = asyncio.Event()
    real_run_in_executor = asyncio.get_running_loop().run_in_executor

    async def slow_import(_executor, func, arg):  # type: ignore[no-untyped-def]
        # Yield so the second caller can observe the future before we resolve.
        await gate.wait()
        return func(arg)

    def fake_get_module(_name: str) -> types.ModuleType:
        return fake

    monkeypatch.setattr(lazy_module, "_get_module", fake_get_module)

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop,
        "run_in_executor",
        lambda executor, func, arg: asyncio.ensure_future(slow_import(executor, func, arg)),
    )

    first = asyncio.create_task(lazy_module.async_import_module(name))
    await asyncio.sleep(0)  # let *first* register its future
    second = asyncio.create_task(lazy_module.async_import_module(name))
    await asyncio.sleep(0)  # let *second* land on the await-future branch
    assert name in lazy_module._futures
    gate.set()

    results = await asyncio.gather(first, second)
    assert results == [fake, fake]
    assert name not in lazy_module._futures

    # Restore so other tests see the real loop method.
    monkeypatch.setattr(loop, "run_in_executor", real_run_in_executor)


async def test_async_import_module_propagates_import_failure() -> None:
    """A failing import surfaces the exception and clears the future."""
    name = "definitely_not_a_real_module_xyz_lazy_test"

    with pytest.raises(ModuleNotFoundError):
        await lazy_module.async_import_module(name)
    assert name not in lazy_module._futures
    assert name not in lazy_module._cache
