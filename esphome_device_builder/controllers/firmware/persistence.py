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
import os
import re
import tempfile
from operator import attrgetter
from pathlib import Path
from typing import TYPE_CHECKING

from esphome.core import CORE

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
    configuration (newest wins) and cap at
    :data:`_MAX_PRIMARY_TERMINAL_JOBS`. Terminal clean / reset
    jobs are kept in a separate pool capped at
    :data:`_MAX_AUX_TERMINAL_JOBS`. Caller persists the result;
    sidecars of dropped jobs are reaped by ``persist_jobs``.
    """
    terminal_states = TERMINAL_JOB_STATUSES

    active: list[FirmwareJob] = []
    primary: list[FirmwareJob] = []
    aux: list[FirmwareJob] = []
    for job in controller.state.jobs.values():
        if job.status not in terminal_states:
            active.append(job)
        elif job.job_type in _PRIMARY_JOB_TYPES:
            primary.append(job)
        else:
            aux.append(job)

    # Sort newest-first so dedup keeps the most recent entry per
    # configuration and the cap retains the most recent N overall.
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

    controller.state.jobs = {j.job_id: j for j in (*active, *deduped_primary, *aux)}


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
    for job_data in data.get(_JOBS_KEY, []):
        try:
            job = FirmwareJob.from_dict(job_data)
            controller.state.jobs[job.job_id] = job
            if job.status in _ACTIVE_JOB_STATUSES:
                if job.status == JobStatus.RUNNING:
                    job.reset()
                job.status = JobStatus.QUEUED
                await controller.state.queue.put(job)
            elif job.output:
                # Legacy blob with inline output on a terminal job:
                # migrate it to a sidecar. Cleared from RAM only after
                # the write lands (in ``_migrate``), so a failed write
                # leaves the output in RAM — where the next
                # ``persist_jobs`` flush saves it — and the inline blob
                # intact, rather than dropping the only copy.
                to_migrate.append(job)
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
            if job.status in TERMINAL_JOB_STATUSES and job.output:
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
    if job.status in TERMINAL_JOB_STATUSES:
        return job_dict_without_output(job)
    return job.to_dict()


def _job_log_path(job_id: str) -> Path:
    """Sidecar log path for *job_id* under ``CORE.data_dir``."""
    return Path(CORE.data_dir) / _JOB_LOG_DIRNAME / f"{job_id}.log"


def _write_job_sidecar(job_id: str, lines: list[str]) -> None:
    """Atomically write *lines* (each carrying its own terminator) to the sidecar."""
    path = _job_log_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{job_id}.", suffix=".tmp")
    try:
        # ``newline=""`` disables universal-newline translation so a
        # bare ``\r`` progress terminator survives the write verbatim.
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write("".join(lines))
        Path(tmp).replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(tmp).unlink()
        raise


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
