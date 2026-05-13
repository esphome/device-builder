"""Firmware-job bulk submission: compile_bulk + install_bulk."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ...helpers.api import CommandError
from ...models import FirmwareJob, JobType
from .helpers import _validate_port

if TYPE_CHECKING:
    from .controller import FirmwareController

_LOGGER = logging.getLogger(__name__)


async def compile_bulk(
    controller: FirmwareController,
    *,
    configurations: list[str],
    force_local: bool = False,
) -> list[FirmwareJob]:
    """Queue compile for *configurations*; skip per-device errors and keep going.

    ``force_local=True`` keeps every job LOCAL (otherwise paired-build
    auto-routing may send some REMOTE).
    """
    await controller._validate_configurations_boundary(configurations)
    jobs: list[FirmwareJob] = []
    for config in configurations:
        try:
            build_source = controller._resolve_install_source(force_local=force_local)
            job = controller._create_job(
                config,
                JobType.COMPILE,
                build_source=build_source,
            )
            await controller._enqueue(job)
        except CommandError as exc:
            _LOGGER.info("Skipping %s in compile_bulk: %s", config, exc.message)
            continue
        jobs.append(job)
    return jobs


async def install_bulk(
    controller: FirmwareController, *, configurations: list[str], port: str = "OTA"
) -> list[FirmwareJob]:
    """Queue install (compile + upload) for *configurations*; defaults to OTA.

    ``port`` is shared across every queued job — pass an explicit IP
    only when every device should install against the same target.
    Per-device errors skip that device and keep going.
    """
    _validate_port(port)
    await controller._validate_configurations_boundary(configurations)
    jobs: list[FirmwareJob] = []
    for config in configurations:
        try:
            build_source = controller._resolve_install_source()
            job = controller._create_job(
                config,
                JobType.INSTALL,
                port=port,
                build_source=build_source,
            )
            await controller._enqueue(job)
        except CommandError as exc:
            _LOGGER.info("Skipping %s in install_bulk: %s", config, exc.message)
            continue
        jobs.append(job)
    return jobs
