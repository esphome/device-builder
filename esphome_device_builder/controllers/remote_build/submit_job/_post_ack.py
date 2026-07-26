"""Post-ack extract, queue, and terminal-frame reporting for ``submit_job``."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from ....helpers.api import CommandError
from ....helpers.async_ import run_in_executor
from ....helpers.lazy_module import async_import_module
from ....helpers.paths import PathEscapeError, resolve_under_root
from ....helpers.remote_build_layout import REMOTE_BUILDS_SUBDIR, RemoteBuildPath
from ....models import ErrorCode, JobStateChangedFrameData
from .const import (
    REASON_EXTRACT_FAILED,
    REASON_INVALID_HEADER,
    REASON_QUEUE_REJECTED,
    TARGET_TO_JOB_TYPE,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ....models import FirmwareJob
    from ..peer_link import PeerLinkSession
    from ._extract_window import ExtractWindow
    from .controller import SubmitJobReceiver, _PendingSubmit

_LOGGER = logging.getLogger(__name__)


async def run_post_ack_extract(
    receiver: SubmitJobReceiver,
    *,
    session: PeerLinkSession,
    pending: _PendingSubmit,
    bundle_bytes: bytes,
) -> None:
    """Extract + queue one accepted bundle, serialized per peer."""
    extracts = receiver._extracts
    key = (session.dashboard_id, pending.job_id)
    try:
        _raise_if_cancel_flagged(extracts, key)
        # The (FIFO) lock keeps per-peer enqueue order and keeps a
        # same-configuration resubmit off a target dir mid-extract.
        async with extracts.lock(session.dashboard_id):
            job = await receiver._extract_and_queue(
                session=session, pending=pending, bundle_bytes=bundle_bytes, cancel_key=key
            )
    except _SubmitJobRejectionError as exc:
        await report_post_ack_failure(session, pending=pending, reason=exc.reason)
    except _ExtractCancelledError:
        await send_job_cancelled(session, job_id=pending.job_id)
    except Exception:
        # The offloader already got ``accepted`` and is waiting on
        # job-state events; an unreported crash strands it forever.
        _LOGGER.exception(
            "submit_job from %s: unexpected extract failure for job %s",
            session.dashboard_id,
            pending.job_id,
        )
        await report_post_ack_failure(session, pending=pending, reason=REASON_EXTRACT_FAILED)
    else:
        # No await between the enqueue and this block (the lock's exit
        # doesn't yield), so dropping the index here leaves no
        # unroutable window. The job is live in the queue; nothing
        # past this point may report ``failed``.
        extracts.handoff(key)
        if extracts.consume(key):
            await cancel_queued_job(receiver, session, pending=pending, job=job)
    finally:
        extracts.consume(key)


async def extract_and_queue(
    receiver: SubmitJobReceiver,
    *,
    session: PeerLinkSession,
    pending: _PendingSubmit,
    bundle_bytes: bytes,
    cancel_key: tuple[str, str],
) -> FirmwareJob:
    """
    Write the tarball, extract it, queue and return the :class:`FirmwareJob`.

    Raises :class:`_SubmitJobRejectionError` with a reason code on failure;
    all disk I/O runs in a single executor hop.
    """
    _raise_if_cancel_flagged(receiver._extracts, cancel_key)
    # Subtree (extract target) + bundle (sibling tarball)
    # flow through the layout helper so the writer here and
    # the 6c sweeper read one source of truth for the
    # on-disk shape. Sibling-not-child is load-bearing:
    # upstream prepare_bundle_for_compile wipes target_dir
    # before extract_bundle reads from bundle_path, so a
    # bundle inside target_dir would be deleted mid-flow
    # (PR #552).
    key = RemoteBuildPath(dashboard_id=session.dashboard_id, device_name=pending.device_stem)
    target_dir = key.subtree(receiver._config_dir)
    bundle_path = key.bundle(receiver._config_dir)
    remote_builds_root = receiver._config_dir / REMOTE_BUILDS_SUBDIR

    # ``esphome.bundle`` is ~1 MB of upstream code; load it through
    # the shared lazy-import executor so the receiver's idle resident
    # set stays lean until a peer-link offload actually lands. Pass
    # the resolved callable into the executor so the worker doesn't
    # rely on a preload-ordering invariant in this caller.
    bundle = await async_import_module("esphome.bundle")
    prepare = bundle.prepare_bundle_for_compile
    try:
        configuration = await run_in_executor(
            _validate_write_extract_bundle,
            bundle_path,
            bundle_bytes,
            target_dir,
            remote_builds_root,
            receiver._config_dir,
            prepare,
        )
    except PathEscapeError as exc:
        # Wire-shape problem (traversal via ``configuration_filename``
        # / ``dashboard_id``), not a receiver-side I/O failure — maps
        # to ``invalid_header``, distinct from ``extract_failed``.
        _LOGGER.warning(
            "submit_job from %s: target_dir %s escaped remote-builds root; rejecting",
            session.dashboard_id,
            target_dir,
        )
        raise _SubmitJobRejectionError(REASON_INVALID_HEADER) from exc
    except (bundle.EsphomeError, OSError) as exc:
        _LOGGER.warning(
            "submit_job from %s: extract failed for job %s (%s): %s",
            session.dashboard_id,
            pending.job_id,
            pending.configuration_filename,
            exc,
        )
        raise _SubmitJobRejectionError(REASON_EXTRACT_FAILED) from exc

    # Snapshot the offloader's display label so the
    # firmware-tasks UI can render "from {label}" without
    # re-looking-up the (potentially since-renamed) peer.
    # Goes through :meth:`ReceiverController.approved_peer_label`
    # so the receiver doesn't couple to the private
    # ``_approved_peers`` layout — a future refactor of the
    # peer registry (e.g. moving APPROVED rows into a per-
    # file ``Store`` like ``_pairings``) only has to keep
    # the accessor's contract.
    remote_peer_label = ""
    receiver_controller = receiver._firmware._db.remote_build_receiver
    if receiver_controller is not None:
        remote_peer_label = receiver_controller.approved_peer_label(session.dashboard_id)

    _raise_if_cancel_flagged(receiver._extracts, cancel_key)
    try:
        job = receiver._firmware._create_job(
            configuration=configuration,
            job_type=TARGET_TO_JOB_TYPE[pending.target],
            remote_peer=session.dashboard_id,
            remote_peer_label=remote_peer_label,
            remote_job_id=pending.job_id,
            # ``device_name`` / ``device_friendly_name`` come
            # off the wire header — the offloader already
            # knows both from its local Device list at install
            # time, so the receiver doesn't re-parse the
            # bundled YAML just to render a title. Defaults
            # to ``""`` for older offloaders that don't set
            # the (NotRequired) fields; the frontend's title
            # then falls back to the configuration path.
            device_name=pending.device_name,
            device_friendly_name=pending.device_friendly_name,
            target_esphome_version=pending.target_esphome_version,
        )
        await receiver._firmware._enqueue(job)
    except Exception as exc:
        _LOGGER.warning(
            "submit_job from %s: enqueue failed for job %s: %s",
            session.dashboard_id,
            pending.job_id,
            exc,
        )
        raise _SubmitJobRejectionError(REASON_QUEUE_REJECTED) from exc

    _LOGGER.info(
        "submit_job from %s: queued job %s (%s, target=%s)",
        session.dashboard_id,
        job.job_id,
        configuration,
        pending.target,
    )
    # No awaits between the enqueue and this return; the caller's
    # handoff relies on it.
    return job


async def cancel_queued_job(
    receiver: SubmitJobReceiver,
    session: PeerLinkSession,
    *,
    pending: _PendingSubmit,
    job: FirmwareJob,
) -> None:
    """
    Route a cancel flagged in the enqueue window through the firmware queue.

    An already-terminal refusal defers to the fan-out's frame; a vanished
    job gets the terminal ``cancelled`` frame directly; any other failure
    sends nothing (the job is live in the queue).
    """
    try:
        await receiver._firmware.cancel(job_id=job.job_id)
    except CommandError as exc:
        if exc.code is ErrorCode.INVALID_ARGS:
            _LOGGER.debug(
                "submit_job from %s: firmware refused post-enqueue cancel for job %s: %s",
                session.dashboard_id,
                job.job_id,
                exc.message,
            )
            return
        _LOGGER.warning(
            "submit_job from %s: post-enqueue cancel for job %s failed (%s): %s",
            session.dashboard_id,
            job.job_id,
            exc.code,
            exc.message,
        )
        await send_terminal_state(
            session,
            job_id=pending.job_id,
            status="cancelled",
            error_message="cancelled after queueing; job no longer present",
        )
    except Exception:
        _LOGGER.exception(
            "submit_job from %s: post-enqueue cancel for job %s crashed; job remains queued",
            session.dashboard_id,
            job.job_id,
        )


async def send_job_cancelled(session: PeerLinkSession, *, job_id: str) -> None:
    """Report a peer-cancelled extract as a terminal ``cancelled`` frame."""
    _LOGGER.info(
        "submit_job from %s: job %s cancelled mid-extract",
        session.dashboard_id,
        job_id,
    )
    await send_terminal_state(
        session,
        job_id=job_id,
        status="cancelled",
        error_message="cancelled by the offloader before the build was queued",
    )


async def send_terminal_state(
    session: PeerLinkSession,
    *,
    job_id: str,
    status: Literal["failed", "cancelled"],
    error_message: str,
) -> None:
    """Send a terminal ``job_state_changed`` frame keyed on the offloader's *job_id*."""
    frame = JobStateChangedFrameData(
        type="job_state_changed",
        job_id=job_id,
        status=status,
        error_message=error_message,
    )
    await session.send_app_frame(dict(frame))


async def report_post_ack_failure(
    session: PeerLinkSession, *, pending: _PendingSubmit, reason: str
) -> None:
    """
    Surface a post-ack extract/queue failure as a terminal ``failed`` frame.

    No :class:`FirmwareJob` reached the queue, so the fan-out won't emit one.
    """
    _LOGGER.warning(
        "submit_job from %s: job %s failed after accept (%s)",
        session.dashboard_id,
        pending.job_id,
        reason,
    )
    await send_terminal_state(
        session,
        job_id=pending.job_id,
        status="failed",
        error_message=f"receiver could not process the bundle: {reason}",
    )


class _SubmitJobRejectionError(Exception):
    """
    Internal: surface a typed rejection reason out of :func:`extract_and_queue`.

    Caught by :func:`run_post_ack_extract` and converted to a terminal
    ``failed`` frame; never leaks past that boundary.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _ExtractCancelledError(Exception):
    """Internal: a peer ``cancel_job`` hit the post-ack extract window."""


def _raise_if_cancel_flagged(extracts: ExtractWindow, key: tuple[str, str]) -> None:
    """Consume any cancellation flag for *key* and raise if one was set."""
    if extracts.consume(key):
        raise _ExtractCancelledError


def _validate_write_extract_bundle(
    bundle_path: Path,
    bundle_bytes: bytes,
    target_dir: Path,
    remote_builds_root: Path,
    config_dir: Path,
    prepare_bundle_for_compile: Callable[[Path, Path], Path],
) -> str:
    """
    Sync helper: validate path, write tarball, extract, return wire-shape ``configuration``.

    The under-root check runs before anything touches disk, so a malicious
    path shape can't materialise even an empty file outside the
    remote-builds subtree. Raises :class:`PathEscapeError` on that branch so
    the caller can distinguish bad input shape from an extract failure;
    ``EsphomeError`` / ``OSError`` propagate untouched.
    """
    # Defence-in-depth re-check of the pre-ack header gate: the
    # header-time resolve can be invalidated by a symlink created
    # under the remote-builds root between ack and extract.
    resolve_under_root(target_dir, remote_builds_root)
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    bundle_path.write_bytes(bundle_bytes)
    extracted: Path = prepare_bundle_for_compile(bundle_path, target_dir)
    # ``as_posix`` keeps the wire-side ``configuration`` string
    # stable across receiver platforms — ``str(rel_yaml)`` would
    # emit ``\\``-separated paths on Windows.
    return extracted.relative_to(config_dir.resolve()).as_posix()
