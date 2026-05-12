"""Free helpers shared between the offloader and receiver siblings.

The two sibling controllers
(:class:`~.offloader.OffloaderController`,
:class:`~.receiver.ReceiverController`) own disjoint state and
disjoint method sets — this module is the *only* legal coupling
point. Kept deliberately small: a place for stateless utilities
that both roles' lifecycles need.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any


async def drain_tasks(tasks: Iterable[asyncio.Task[Any]]) -> None:
    """Cancel and await every task in *tasks*, swallowing exceptions.

    Snapshots *tasks* to a list so the caller's post-drain
    ``clear`` doesn't pull tasks out from under the gather.
    Caller owns clearing the source collection.
    """
    tasks_list = list(tasks)
    if not tasks_list:
        return
    for task in tasks_list:
        task.cancel()
    await asyncio.gather(*tasks_list, return_exceptions=True)
