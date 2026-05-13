"""
Firmware-job persistence: load on startup, prune history, save on transition.

Free functions taking :class:`FirmwareController` as the first
arg (the canonical devices/firmware_sync.py pattern). The
controller exposes thin ``_load_jobs`` / ``_persist_jobs`` /
``_prune_history`` methods so existing test patches on those
bound names keep working.
"""

from __future__ import annotations

import asyncio
import logging
from operator import attrgetter
from typing import TYPE_CHECKING

from ...models import TERMINAL_JOB_STATUSES, FirmwareJob, JobStatus
from ..config import _load_metadata, metadata_transaction
from .constants import (
    _ACTIVE_JOB_STATUSES,
    _JOBS_KEY,
    _MAX_AUX_TERMINAL_JOBS,
    _MAX_PRIMARY_TERMINAL_JOBS,
    _PRIMARY_JOB_TYPES,
)

if TYPE_CHECKING:
    from .controller import FirmwareController

_LOGGER = logging.getLogger(__name__)


def prune_history(controller: FirmwareController) -> None:
    """
    Trim ``controller._jobs`` to the configured history limits.

    Active (queued / running) jobs are always kept. Terminal
    compile / upload / install jobs collapse to one entry per
    configuration (newest wins) and cap at
    :data:`_MAX_PRIMARY_TERMINAL_JOBS`. Terminal clean / reset
    jobs are kept in a separate pool capped at
    :data:`_MAX_AUX_TERMINAL_JOBS`. Caller persists the result.
    """
    terminal_states = TERMINAL_JOB_STATUSES

    active: list[FirmwareJob] = []
    primary: list[FirmwareJob] = []
    aux: list[FirmwareJob] = []
    for job in controller._jobs.values():
        if job.status not in terminal_states:
            active.append(job)
        elif job.job_type in _PRIMARY_JOB_TYPES:
            primary.append(job)
        else:
            aux.append(job)

    # Sort newest-first so dedup keeps the most recent entry per
    # device and the cap retains the most recent N overall.
    primary.sort(key=attrgetter("created_at"), reverse=True)
    seen_configs: set[str] = set()
    deduped_primary: list[FirmwareJob] = []
    for job in primary:
        if job.configuration:
            if job.configuration in seen_configs:
                continue
            seen_configs.add(job.configuration)
        deduped_primary.append(job)
    deduped_primary = deduped_primary[:_MAX_PRIMARY_TERMINAL_JOBS]

    aux.sort(key=attrgetter("created_at"), reverse=True)
    aux = aux[:_MAX_AUX_TERMINAL_JOBS]

    controller._jobs = {j.job_id: j for j in (*active, *deduped_primary, *aux)}


async def load_jobs(controller: FirmwareController) -> None:
    """
    Load persisted jobs and re-queue any incomplete ones.

    QUEUED and RUNNING both re-queue — the user asked for the
    build, and a rebuild-and-reflash is idempotent (worst case
    they pay a couple minutes of compile time on identical
    firmware). RUNNING goes through :meth:`FirmwareJob.reset`
    first so the per-run fields (progress / error / timestamps /
    exit_code) look fresh, but the pre-crash ``output`` log is
    kept as diagnostic history with a separator marker showing
    where the rebuild starts. Terminal jobs load into the
    in-memory map for the recent-jobs panel but don't touch
    ``_queue``. See esphome/device-builder#147 for the policy.
    """
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, _load_metadata, controller._db.settings.config_dir)
    for job_data in data.get(_JOBS_KEY, []):
        try:
            job = FirmwareJob.from_dict(job_data)
            controller._jobs[job.job_id] = job
            if job.status in _ACTIVE_JOB_STATUSES:
                if job.status == JobStatus.RUNNING:
                    job.reset()
                job.status = JobStatus.QUEUED
                await controller._queue.put(job)
        except Exception:
            # ``job_data`` is normally a dict, but a corrupt
            # persistence file could contain a primitive
            # (string, int, ``None``) where a dict was expected.
            # ``.get`` would raise ``AttributeError`` on those,
            # defeating the "skip and continue" intent of this
            # branch. Probe by isinstance and fall back to the
            # raw repr.
            identity = (
                job_data.get("job_id", "?")
                if isinstance(job_data, dict)
                else f"<non-dict entry: {job_data!r}>"
            )
            _LOGGER.warning("Failed to restore job: %s", identity, exc_info=True)


async def persist_jobs(controller: FirmwareController) -> None:
    """Save all jobs to disk."""
    loop = asyncio.get_running_loop()
    config_dir = controller._db.settings.config_dir

    def _save() -> None:
        with metadata_transaction(config_dir) as data:
            data[_JOBS_KEY] = [j.to_dict() for j in controller._jobs.values()]

    await loop.run_in_executor(None, _save)
