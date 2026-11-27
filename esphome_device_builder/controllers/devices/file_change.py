"""Multi-file YAML changes for the devices controller (package create / delete / rename)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ...helpers.api import CommandError
from ...helpers.async_ import run_in_executor
from ...helpers.uncancellable import run_to_completion
from ...models import ErrorCode
from ..version_history import GIT_COMMIT_ERRORS
from .write_locks import yaml_write_locks

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
    precheck: Callable[[], None] | None = None,
    snapshot: bool = True,
) -> None:
    """
    Run blocking *change* under the write locks of *configurations*, then record history.

    Blocking *precheck* runs first under the locks; a refusal it raises records
    nothing. ``UNAVAILABLE`` when the *snapshot* of the files before the change fails.
    """

    async def _locked() -> None:
        async with yaml_write_locks(controller, configurations):
            if precheck is not None:
                await run_in_executor(precheck)
            if snapshot:
                # The history baseline seeds top-level YAMLs only: record an untracked
                # package before a delete or rename removes its only copy (no-op otherwise).
                await _commit_history_many(
                    controller, configurations, f"Before: {message}", strict=True
                )
            try:
                await run_in_executor(change)
                await _commit_history_many(controller, configurations, message)
            finally:
                # Also after a change that failed part-way: some files may be written.
                for configuration in configurations:
                    controller._after_yaml_write(configuration)

    await run_to_completion(_locked(), name=message)


async def _commit_history_many(
    controller: DevicesController,
    configurations: Sequence[str],
    message: str,
    *,
    strict: bool = False,
) -> None:
    """Record *configurations* in one version-history commit; swallow git errors unless *strict*."""
    version_history = controller._db.version_history
    if version_history is None:
        return
    rel_path = controller._db.settings.rel_path
    paths = await run_in_executor(lambda: [rel_path(c) for c in configurations])
    try:
        await version_history.commit(paths, message)
    except GIT_COMMIT_ERRORS as err:
        if strict:
            msg = "version history couldn't record the files first; nothing was changed"
            raise CommandError(ErrorCode.UNAVAILABLE, msg) from err
        _LOGGER.exception("Version-history commit failed for %s", ", ".join(configurations))
        return
    for configuration in configurations:
        version_history.discard_pending(configuration)
