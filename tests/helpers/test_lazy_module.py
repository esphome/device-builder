"""Coverage for ``helpers.lazy_module.async_import_module``."""

from __future__ import annotations

import sys
import types

import pytest

from esphome_device_builder.helpers import lazy_module


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    """Wipe the per-module cache between tests."""
    lazy_module._cache.clear()


async def test_async_import_module_returns_cached_on_repeat_call() -> None:
    """Once cached, repeat callers skip the executor entirely."""
    name = "esphome_device_builder._test_lazy_cached"
    fake = types.ModuleType(name)
    lazy_module._cache[name] = fake

    result = await lazy_module.async_import_module(name)
    assert result is fake


async def test_async_import_module_first_call_imports_via_executor() -> None:
    """A first-time call runs the importlib hop and caches the module."""
    # ``http.cookiejar`` is in stdlib but rarely preloaded; pop it so the
    # executor branch actually runs even if pytest has touched it.
    name = "http.cookiejar"
    sys.modules.pop(name, None)

    result = await lazy_module.async_import_module(name)
    assert result is sys.modules[name]
    assert lazy_module._cache[name] is result
    assert hasattr(result, "CookieJar")  # sanity: real module, not a stub


async def test_async_import_module_propagates_import_failure() -> None:
    """A failing import surfaces the exception; nothing is cached."""
    name = "definitely_not_a_real_module_xyz_lazy_test"

    with pytest.raises(ModuleNotFoundError):
        await lazy_module.async_import_module(name)
    assert name not in lazy_module._cache
