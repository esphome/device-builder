"""Per-file YAML write locks for the devices controller."""

from __future__ import annotations

import os
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable

    from .controller import DevicesController


def yaml_lock_key(configuration: str) -> str:
    """Return the lock key every spelling of *configuration* shares."""
    # Lexical (``foo/../kitchen.yaml``) and case-folded: on Windows and macOS
    # ``Kitchen.yaml`` is the same file, so it must not get a lock of its own.
    return os.path.normcase(os.path.normpath(configuration)).casefold()


@asynccontextmanager
async def yaml_write_locks(
    controller: DevicesController, configurations: Iterable[str]
) -> AsyncIterator[None]:
    """Hold the write locks of *configurations*, each lock once, in a stable order."""
    async with AsyncExitStack() as stack:
        # Sorted, so two multi-file writers can't take the locks in opposite orders;
        # deduped by key, as ``asyncio.Lock`` isn't reentrant.
        for key in sorted({yaml_lock_key(c) for c in configurations}):
            await stack.enter_async_context(controller._yaml_write_lock(key))
        yield
