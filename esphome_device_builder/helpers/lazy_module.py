"""Off-loop module loading with a dedicated single-thread executor.

Concurrent imports across threads aren't safe in CPython before
3.15; an executor pool that runs more than one import in parallel
can deadlock or observe half-initialised module state. Mirroring
``homeassistant.helpers.importlib``, this helper routes every lazy
import through a ``ThreadPoolExecutor(max_workers=1)`` so only one
import is ever in flight, and dedupes concurrent callers through a
per-module future.

Used to keep heavy esphome subpackages (``dashboard_import``,
``components.esp32``, ``components.libretiny``, ``bundle``) out of
the dashboard's resident set when the corresponding feature isn't
exercised in a session.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from concurrent.futures import ThreadPoolExecutor
from types import ModuleType

_import_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ImportExecutor")
_cache: dict[str, ModuleType] = {}
_futures: dict[str, asyncio.Future[ModuleType]] = {}


def _get_module(name: str) -> ModuleType:
    module = importlib.import_module(name)
    _cache[name] = module
    return module


async def async_import_module(name: str) -> ModuleType:
    """Import *name* off the event loop, deduping concurrent calls.

    First caller submits the import to the dedicated single-thread
    executor; concurrent callers await the same future. Subsequent
    calls hit the in-process cache directly.
    """
    if (module := _cache.get(name)) is not None:
        return module
    if (future := _futures.get(name)) is not None:
        return await future
    if name in sys.modules:
        cached = sys.modules[name]
        _cache[name] = cached
        return cached

    loop = asyncio.get_running_loop()
    future = loop.create_future()
    _futures[name] = future
    try:
        module = await loop.run_in_executor(_import_executor, _get_module, name)
        future.set_result(module)
    except BaseException as ex:
        future.set_exception(ex)
        future.exception()  # mark retrieved so a sole consumer doesn't warn
        raise
    finally:
        del _futures[name]
    return module
