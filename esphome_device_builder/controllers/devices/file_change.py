"""Multi-file YAML changes for the devices controller (package create / delete / rename)."""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING

from ...helpers.async_ import run_in_executor
from ..version_history import GIT_COMMIT_ERRORS

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from .controller import DevicesController

_LOGGER = logging.getLogger(__name__)


async def commit_file_change(
    controller: DevicesController,
    configurations: Sequence[str],
    change: Callable[[], None],
    *,
    message: str,
) -> None:
    """Run blocking *change* under the write locks of *configurations*, then record history."""
    async with AsyncExitStack() as stack:
        # Sorted, so two multi-file writers can't take the locks in opposite orders.
        for configuration in sorted(set(configurations)):
            await stack.enter_async_context(controller._yaml_write_lock(configuration))
        await run_in_executor(change)
        await _commit_history_many(controller, configurations, message)
    for configuration in configurations:
        controller._after_yaml_write(configuration)


async def _commit_history_many(
    controller: DevicesController, configurations: Sequence[str], message: str
) -> None:
    """Record *configurations* in one version-history commit; swallow genuine git errors."""
    version_history = controller._db.version_history
    if version_history is None:
        return
    paths = [controller._db.settings.rel_path(c) for c in configurations]
    try:
        await version_history.commit(paths, message)
    except GIT_COMMIT_ERRORS:
        _LOGGER.exception("Version-history commit failed for %s", ", ".join(configurations))
        return
    for configuration in configurations:
        version_history.discard_pending(configuration)
