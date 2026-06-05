"""
Firmware-job persistence: load on startup, prune history, save on transition.

Job *metadata* lives in the ``.device-builder.json`` blob; job
*output* lives in per-job sidecar logs under
``CORE.data_dir/dashboard-jobs/<job_id>.log`` so the ~2000-line build
log of every retained terminal job isn't held in RAM (or reloaded
into RAM at startup). Output stays in RAM only while a job is live;
on the terminal transition it's flushed to its sidecar and dropped
from RAM. ``follow_job`` replays a terminal job's log from disk.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from operator import attrgetter
from pathlib import Path
from typing import TYPE_CHECKING

from esphome.core import CORE

from ...helpers.atomic_io import atomic_write
from ...models import (
    FirmwareJob,
    JobStatus,
    JobType,
)
from ..config import _load_metadata, metadata_transaction
from .constants import (
    _JOBS_KEY,
    _MAX_AUX_TERMINAL_JOBS,
    _MAX_PRIMARY_TERMINAL_JOBS,
    _PREREQUISITE_FAILED_ERROR,
    _PRIMARY_JOB_TYPES,
)

if TYPE_CHECKING:
    from .controller import FirmwareController

_LOGGER = logging.getLogger(__name__)

_JOB_LOG_DIRNAME = "dashboard-jobs"

# One output line = run of non-terminator chars plus a single ``\n`` or
# ``\r`` terminator, or a trailing run with none. Matches the ingest
# split (``\n`` / ``\r`` only) so write→read round-trips exactly, unlike
# ``str.splitlines`` which also breaks on form-feed / Unicode separators.
_LINE_RE = re.compile(r"[^\r\n]*[\r\n]|[^\r\n]+")


def prune_history(controller: FirmwareController) -> None:
    """
    Trim ``controller.state.jobs`` to the configured history limits.

    Active (queued / running) jobs are always kept. Terminal
    compile / upload / install jobs collapse to one entry per
    (configuration, job type) — newest wins — and cap at
    :data:`_MAX_PRIMARY_TERMINAL_JOBS`. Keying on type as well as
    config keeps both halves of an install (a COMPILE and its
    dependent UPLOAD share a config) so the build log stays
    reachable, not just the flash log. Terminal clean / reset
    jobs are kept in a separate pool capped at
    :data:`_MAX_AUX_TERMINAL_JOBS`. Caller persists the result;
    sidecars of dropped jobs are reaped by ``persist_jobs``.
    """
    active: list[FirmwareJob] = []
    primary: list[FirmwareJob] = []
    aux: list[FirmwareJob] = []
    for job in controller.state.jobs.values():
        if not job.is_terminal:
            active.append(job)
        elif job.job_type in _PRIMARY_JOB_TYPES:
            primary.append(job)
        else:
            aux.append(job)

    # Sort newest-first so dedup keeps the most recent entry per
    # (configuration, type) and the cap retains the most recent N overall.
    primary.sort(key=attrgetter("created_at"), reverse=True)
    seen: set[tuple[str, JobType]] = set()
    deduped_primary: list[FirmwareJob] = []
    for job in primary:
        if job.configuration:
            key = (job.configuration, job.job_type)
            if key in seen:
                continue
            seen.add(key)
        deduped_primary.append(job)
    deduped_primary = deduped_primary[:_MAX_PRIMARY_TERMINAL_JOBS]

    aux.sort(key=attrgetter("created_at"), reverse=True)
    aux = aux[:_MAX_AUX_TERMINAL_JOBS]

    controller.state.jobs = {j.job_id: j for j in (*active, *deduped_primary, *aux)}


def _restore_job_entry(
    controller: FirmwareController,
    job_data: object,
    active: list[FirmwareJob],
    to_migrate: list[FirmwareJob],
) -> None:
    """Deserialise one persisted entry into ``state.jobs``; classify it.

    Active (QUEUED/RUNNING) jobs flip to QUEUED (RUNNING via ``reset()``)
    and collect into *active* for lane routing; a terminal job still
    carrying inline ``output`` (legacy blob) collects into *to_migrate*
    for sidecar migration. A corrupt entry is logged and skipped.
    """
    try:
        job = FirmwareJob.from_dict(job_data)  # type: ignore[arg-type]
        controller.state.jobs[job.job_id] = job
        if job.is_active:
            job.restore_for_requeue()
            active.append(job)
        elif job.output:
            # Cleared from RAM only after the sidecar write lands, so a
            # failed write leaves the output for the next persist flush.
            to_migrate.append(job)
    except Exception:
        # A corrupt file could hold a primitive where a dict was expected;
        # ``.get`` would raise, defeating skip-and-continue. Probe type.
        identity = (
            job_data.get("job_id", "?")
            if isinstance(job_data, dict)
            else f"<non-dict entry: {job_data!r}>"
        )
        _LOGGER.warning("Failed to restore job: %s", identity, exc_info=True)


def _restore_to_lane(controller: FirmwareController, job: FirmwareJob) -> None:
    """Route a restored active *job* to its lane, hold it, or cancel it.

    No prerequisite (or one already completed) → onto its lane. A
    prerequisite still pending → held; ``lifecycle._release_dependents``
    lands it when the prerequisite finishes. A prerequisite that's gone or
    didn't succeed → the dependent can't run, so cancel it. A dependent
    already released (``dependency_released``) routes onto its lane regardless.
    """
    if controller.state.dependency_satisfied(job) or job.dependency_released:
        controller.state.place_on_lane(job)
        return
    prereq = controller.state.jobs.get(job.depends_on)
    if prereq is not None and prereq.is_active:
        return
    # Prerequisite is gone (pruned from history) or terminal-but-not-completed:
    # the dependent can't run, so cancel it rather than hold it forever. Log so
    # a prereq that was pruned despite succeeding is diagnosable (not silent).
    prereq_status = prereq.status.value if prereq is not None else "missing"
    _LOGGER.info(
        "Cancelling restored job %s: prerequisite %s is %s",
        job.job_id,
        job.depends_on,
        prereq_status,
    )
    job.mark_terminal(JobStatus.CANCELLED, error=_PREREQUISITE_FAILED_ERROR)


async def load_jobs(controller: FirmwareController) -> None:
    """
    Load persisted job metadata and re-queue any incomplete ones.

    Output is not loaded into RAM: terminal jobs deserialise with an
    empty ``output`` (their log lives in the sidecar). QUEUED and
    RUNNING re-queue; RUNNING goes through :meth:`FirmwareJob.reset`
    first to clear per-run state. A legacy blob that still carries
    inline ``output`` on terminal jobs is migrated to sidecars here.
    """
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, _load_metadata, controller._db.settings.config_dir)
    to_migrate: list[FirmwareJob] = []
    # First pass restores every job into the map and flips active ones to
    # QUEUED; the second pass routes them to a lane, so a dependent's
    # prerequisite resolves regardless of on-disk order.
    active: list[FirmwareJob] = []
    for job_data in data.get(_JOBS_KEY, []):
        _restore_job_entry(controller, job_data, active, to_migrate)

    for job in active:
        _restore_to_lane(controller, job)

    if to_migrate:

        def _migrate() -> None:
            # Isolate per job: one failed write (disk full, EACCES)
            # logs and skips that job — its output stays in RAM for the
            # next persist flush — without aborting the batch or
            # blocking startup.
            for job in to_migrate:
                try:
                    _write_job_sidecar(job.job_id, job.output)
                    job.output = []
                except OSError:
                    _LOGGER.warning(
                        "Failed to migrate job %s output to sidecar", job.job_id, exc_info=True
                    )

        await loop.run_in_executor(None, _migrate)


async def persist_jobs(controller: FirmwareController) -> None:
    """Flush terminal-job output to sidecars, then save job metadata.

    Serialized through ``controller._persist_lock`` and the job
    snapshot is taken under it, so concurrent callers can't let an
    older snapshot's executor write land after a newer one's and drop
    jobs from the blob (or reap a sidecar a newer job just wrote).
    """
    async with controller._persist_lock:
        await _persist_jobs_locked(controller)


async def _persist_jobs_locked(controller: FirmwareController) -> None:
    loop = asyncio.get_running_loop()
    config_dir = controller._db.settings.config_dir
    jobs = list(controller.state.jobs.values())

    def _save() -> None:
        # Flush each terminal job's RAM buffer to its sidecar, then
        # drop it from RAM so idle memory holds metadata only. Runs
        # before ``to_dict`` so the persisted blob carries no output.
        for job in jobs:
            if job.is_terminal and job.output:
                _write_job_sidecar(job.job_id, job.output)
                job.output = []
        _reconcile_sidecars({job.job_id for job in jobs})
        with metadata_transaction(config_dir) as data:
            data[_JOBS_KEY] = [_metadata_dict(job) for job in jobs]

    await loop.run_in_executor(None, _save)


def job_dict_without_output(job: FirmwareJob) -> dict:
    """Serialise *job* dropping ``output`` (it's persisted in / served from the sidecar)."""
    data = job.to_dict()
    data.pop("output", None)
    return data


def read_job_output(job_id: str) -> list[str]:
    r"""
    Return a job's persisted output lines (terminators preserved), or ``[]``.

    ``newline=""`` mirrors the write side so universal-newline
    translation doesn't rewrite a bare ``\r`` terminator to ``\n``;
    :data:`_LINE_RE` then re-splits on exactly the ``\n`` / ``\r``
    boundaries the ingest path produced (``str.splitlines`` would also
    break on form-feed and other Unicode line boundaries, splitting a
    line the writer kept whole). A missing sidecar is the normal absent-output case
    and maps to ``[]``; any other read error is logged (and also
    yields ``[]``) so a genuinely unreadable log surfaces in the logs
    instead of masquerading as a job with no output.
    """
    try:
        with _job_log_path(job_id).open(encoding="utf-8", newline="") as fh:
            text = fh.read()
    except FileNotFoundError:
        return []
    except OSError:
        _LOGGER.warning("Failed to read job output sidecar for %s", job_id, exc_info=True)
        return []
    return _LINE_RE.findall(text)


def _metadata_dict(job: FirmwareJob) -> dict:
    """
    Serialise *job* for the metadata blob, dropping ``output`` for terminal jobs.

    Active (queued / running) jobs keep their output inline so a
    mid-build restart recovers the pre-crash log; there are no active
    jobs at idle, so this doesn't bloat the resting blob.
    """
    if job.is_terminal:
        return job_dict_without_output(job)
    return job.to_dict()


def _job_log_path(job_id: str) -> Path:
    """Sidecar log path for *job_id* under ``CORE.data_dir``."""
    return Path(CORE.data_dir) / _JOB_LOG_DIRNAME / f"{job_id}.log"


def _write_job_sidecar(job_id: str, lines: list[str]) -> None:
    r"""Atomically write *lines* (each carrying its own terminator) to the sidecar.

    Encodes to UTF-8 bytes and writes binary so no newline translation
    can rewrite a bare ``\r`` progress terminator.
    """
    atomic_write(_job_log_path(job_id), "".join(lines).encode("utf-8"), make_parents=True)


def _reconcile_sidecars(valid_ids: set[str]) -> None:
    """Delete sidecar logs whose job is no longer retained, plus stale temp files.

    Reaps ``.log`` files for pruned / cleared jobs and any leftover
    ``.tmp`` staging files (a hard kill between ``mkstemp`` and
    ``replace`` orphans one; the normal failure path unlinks its own).
    Runs inside the persist lock after this persist's writes have all
    landed, so no live ``.tmp`` of ours is in flight here.
    """
    log_dir = Path(CORE.data_dir) / _JOB_LOG_DIRNAME
    try:
        entries = list(log_dir.iterdir())
    except FileNotFoundError:
        return  # no jobs persisted yet — nothing to reap
    except OSError:
        _LOGGER.warning("Failed to scan job-log dir %s for reaping", log_dir, exc_info=True)
        return
    for entry in entries:
        stale_log = entry.suffix == ".log" and entry.stem not in valid_ids
        if stale_log or entry.suffix == ".tmp":
            with contextlib.suppress(OSError):
                entry.unlink()
