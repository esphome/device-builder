"""Off-loop module loading with a dedicated single-thread executor.

Concurrent imports across threads aren't safe in CPython before
3.15; an executor pool that runs more than one import in parallel
can deadlock or observe half-initialised module state. This helper
routes every lazy import through a ``ThreadPoolExecutor(max_workers=1)``
so only one import is ever in flight, then caches the resolved
module so steady-state callers skip the executor hop entirely.

Two concurrent first-callers each submit their own ``importlib``
hop; the second runs after the first has populated ``sys.modules``
and so is a hash-table lookup. The simpler shape avoids the
shared-future / first-caller-cancellation poisoning concerns of
the future-dedup variant (cf. ``homeassistant.helpers.importlib``)
while keeping the single-thread import guarantee.

Used to keep heavy esphome subpackages (``dashboard_import``,
``bundle``) out of the dashboard's resident set when the
corresponding feature isn't exercised in a session.
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
    """Import *name* off the event loop and cache the resolved module.

    Steady-state callers hit ``_cache`` directly. Cold callers hop
    onto the dedicated single-thread import executor; concurrent
    first-callers serialise on the executor pool and the second
    run finds the module already in ``sys.modules``.
    """
    if (module := _cache.get(name)) is not None:
        return module
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_import_executor, _get_module, name)
