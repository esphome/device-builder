"""
Lazy module loader: one in-flight import at a time, cached on success.

Routes every cold import through a ``ThreadPoolExecutor(max_workers=1)``
so concurrent first-callers serialise and only one ``importlib``
call runs at a time (pre-3.15 concurrent-import safety).
"""

from __future__ import annotations

import asyncio
import importlib
from concurrent.futures import ThreadPoolExecutor
from types import ModuleType

_import_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ImportExecutor")
_cache: dict[str, ModuleType] = {}


def _get_module(name: str) -> ModuleType:
    module = importlib.import_module(name)
    _cache[name] = module
    return module


async def async_import_module(name: str) -> ModuleType:
    """Return *name* from the cache, importing it off-loop on miss."""
    if (module := _cache.get(name)) is not None:
        return module
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_import_executor, _get_module, name)
