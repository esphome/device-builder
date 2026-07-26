"""Shared vocabulary for the receiver-side ``submit_job`` package."""

from __future__ import annotations

from ....models import JobType

# Reject reason codes carried on
# :class:`SubmitJobAckFrameData.reason` when ``accepted=False``.
# Distinct from :class:`BundleAssemblerErrorCode` (wire-level
# bundle problems): these cover the receiver-side dispatch path
# where the bundle assembled cleanly but something else went
# wrong (path traversal, extraction failure, queue rejection).
# The offloader's submitter maps these to user-facing error
# messages.
REASON_DUPLICATE_SUBMIT = "duplicate_submit"
REASON_INVALID_HEADER = "invalid_header"
REASON_INVALID_CHUNK = "invalid_chunk"
REASON_NO_INFLIGHT = "no_inflight_submit"
REASON_JOB_ID_MISMATCH = "job_id_mismatch"
REASON_CHUNK_DECODE_FAILED = "chunk_decode_failed"
REASON_EXTRACT_FAILED = "extract_failed"
REASON_QUEUE_REJECTED = "queue_rejected"
REASON_UPLOAD_UNSUPPORTED = "upload_unsupported"

# Allowed values of :attr:`SubmitJobFrameData.target`.
# ``Literal["compile", "upload", "clean"]`` on the TypedDict is
# the type-time gate; this set is the runtime gate so a
# misbehaving offloader sending ``target="install"`` (or anything
# else) gets a clean reject rather than a downstream JobType
# construction failure.
#
# ``target="clean"`` rides the same submit_job pipeline as
# compile — receiver re-extracts the YAML to the
# per-offloader subtree, then runs ``esphome clean`` against it,
# which wipes ``<data_dir>/build/<device_name>/``. The receiver's
# 6c TTL sweep eventually reclaims the subtree itself; an
# explicit clean is about freeing the shared per-device build
# tree, not the per-offloader sidecar. The offloader fans out
# clean to every connected peer when the operator clicks "Clean
# build files" so receivers that have built this device locally
# also drop their stale artifacts.
# ``target="upload"`` is deliberately absent — rejected with
# ``upload_unsupported`` in :meth:`SubmitJobReceiver.handle_submit_job`.
TARGET_TO_JOB_TYPE: dict[str, JobType] = {
    "compile": JobType.COMPILE,
    "clean": JobType.CLEAN,
}
