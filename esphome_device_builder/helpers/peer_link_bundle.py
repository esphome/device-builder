"""
Bundle chunking + reassembly helpers for the peer-link ``submit_job`` flow.

The offloader produces a gzipped tarball via
:class:`esphome.bundle.ConfigBundleCreator`; that's a single
``bytes`` payload that has to ride the peer-link's per-frame
size cap (:data:`APP_FRAME_MAX_BYTES`, 32 KiB at 5a-1).
:func:`chunk_bundle` slices the bundle into the wire-format's
base64 envelope shape; :class:`BundleAssembler` does the
reverse on the receiver side, with structured rejection of
the misbehaviours that would otherwise corrupt the on-disk
extract: out-of-order chunks, duplicates, oversized aggregate,
post-completion feed, hash mismatch.

Pure helpers — no controller / WS state. The wire format is
shared by the receiver and offloader, so the helpers live here
rather than in either side's controller module. Tests live in
``tests/test_peer_link_bundle.py``.

Design notes:

* Chunks are base64-encoded inside JSON frames rather than
  riding a parallel binary path. Trade-off described in
  :class:`AppMessageType`'s docstring.
* The chunk-size budget targets ~75 % of
  :data:`APP_FRAME_MAX_BYTES` after b64 inflation + JSON
  envelope overhead, so a chunk_size of 16 KiB raw produces
  a wire frame around 22 KiB. Pinned to a constant rather
  than computed from the cap so a future cap-bump doesn't
  silently change the on-the-wire chunk shape.
* The assembler enforces the offloader's announced
  ``num_chunks`` and ``total_bundle_bytes`` from the
  ``submit_job`` header. A drift between announced and
  observed is the offloader misbehaving (or a wire
  truncation that AEAD didn't catch); raise a structured
  error so the receiver can ``terminate`` the session with
  ``MALFORMED_FRAME``.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Iterator
from enum import StrEnum
from typing import NoReturn

# Raw bytes per chunk before b64 encoding. Sized so the
# resulting JSON frame fits comfortably under
# :data:`APP_FRAME_MAX_BYTES` (60 KiB after 5c-1's bump):
# 32 KiB raw -> ~43 KiB base64 -> ~43.5 KiB JSON envelope
# (incl. ``type``, ``job_id``, ``chunk_index``, ``is_last``
# field overhead). Leaves ~16 KiB headroom for unusually
# long ``job_id`` strings and future header fields. Halving
# the chunk count vs. the original 16 KiB sizing cuts the
# fixed per-frame overhead (Noise AEAD tag + JSON envelope)
# in half on a typical ESPHome bundle.
BUNDLE_CHUNK_SIZE_BYTES = 32 * 1024

# Hard cap on the assembled bundle. ESPHome bundles in the
# wild are 5-50 KiB compressed; an exotic image-heavy include
# tree can push to a few hundred KiB. 4 MiB is well above the
# realistic ceiling but small enough that a misbehaving
# offloader can't pin gigabytes of memory pretending to send
# a bundle. May be revisited based on production bundle sizes.
BUNDLE_MAX_TOTAL_BYTES = 4 * 1024 * 1024

# Hard cap on the assembled firmware binary. Typical
# ESP32 firmware is 800 KiB - 1.5 MiB; ESP32-S3 with PSRAM
# can reach ~4 MiB; future variants may grow further. 16 MiB
# is well above the realistic ceiling but bounded enough that
# a misbehaving receiver can't pin arbitrary memory pretending
# to send firmware. Same buffer-size rationale as
# :data:`BUNDLE_MAX_TOTAL_BYTES`, just with the larger cap
# the firmware-binary direction needs.
FIRMWARE_MAX_TOTAL_BYTES = 16 * 1024 * 1024


class BundleAssemblerErrorCode(StrEnum):
    """
    Reason a :class:`BundleAssembler` rejected a chunk.

    Surfaced on :class:`BundleAssemblerError` so the receiver-
    side dispatch can map to a typed
    :class:`SubmitJobAckFrameData.reason` and a matching
    ``terminate{reason: malformed_frame}`` close.
    """

    OUT_OF_ORDER = "out_of_order"
    POST_COMPLETION = "post_completion"
    OVERSIZED = "oversized"
    UNDERSIZED = "undersized"
    HASH_MISMATCH = "hash_mismatch"
    CHUNK_COUNT_MISMATCH = "chunk_count_mismatch"
    EMPTY_BUNDLE = "empty_bundle"


class BundleAssemblerError(ValueError):
    """A :class:`BundleAssembler` rejected a chunk or the finalised stream."""

    def __init__(self, code: BundleAssemblerErrorCode, message: str) -> None:
        super().__init__(f"{code.value}: {message}")
        self.code = code


# Module-local shorthands so the assembler's raise sites stay readable
# at 100 cols. ``_Code`` is the error-code enum; ``_fail`` raises the
# matching :class:`BundleAssemblerError` and returns ``NoReturn`` so
# mypy treats every callsite as terminating control flow.
_Code = BundleAssemblerErrorCode


def _fail(code: BundleAssemblerErrorCode, message: str) -> NoReturn:
    raise BundleAssemblerError(code, message)


def chunk_bundle(
    data: bytes,
    *,
    chunk_size: int = BUNDLE_CHUNK_SIZE_BYTES,
) -> Iterator[tuple[int, bytes, bool]]:
    """Yield ``(chunk_index, raw_bytes_slice, is_last)`` for every slice of *data*.

    Sliced sequentially from the start; the last slice may be
    shorter than ``chunk_size``. ``chunk_index`` is 0-based and
    monotonic; ``is_last`` is set on the final slice (regardless
    of whether it was a full or partial chunk).

    An empty *data* yields no chunks — the caller must reject
    an empty bundle at the header layer (an empty tarball isn't
    a legitimate ESPHome bundle, even if the wire format would
    technically accept "0 chunks, 0 bytes"). Non-positive
    *chunk_size* raises ``ValueError`` immediately so the
    contract failure surfaces at the call site, not inside the
    iterator on its first ``next()`` — the validation lives
    outside the inner generator on purpose.

    Pure generator — no state outside the local frame; safe to
    drive from an async send loop without touching the bundle
    bytes after a partial yield.
    """
    if chunk_size <= 0:
        raise ValueError(f"chunk_size must be positive; got {chunk_size}")
    return _chunk_bundle_iter(data, chunk_size)


def _chunk_bundle_iter(data: bytes, chunk_size: int) -> Iterator[tuple[int, bytes, bool]]:
    """Inner generator for :func:`chunk_bundle`; validation done by the caller."""
    total = len(data)
    if total == 0:
        return
    index = 0
    offset = 0
    while offset < total:
        end = offset + chunk_size
        is_last = end >= total
        yield index, data[offset:end], is_last
        index += 1
        offset = end


def encode_chunk(raw: bytes) -> str:
    """Base64-encode *raw* for the JSON envelope (URL-safe omitted; standard b64)."""
    return base64.b64encode(raw).decode("ascii")


def decode_chunk(b64_text: str) -> bytes:
    """Base64-decode an inbound chunk's ``data_b64`` field.

    Raises :class:`BundleAssemblerError` with
    :attr:`BundleAssemblerErrorCode.OUT_OF_ORDER` on a
    malformed envelope. ``OUT_OF_ORDER`` here is a misnomer
    relative to the literal cause (decode failure), but the
    receiver-side dispatch treats every assembler rejection as
    a structured-malformed-frame close and the specific code
    only differentiates the log line; collapsing the rare
    decode-failure branch into ``OUT_OF_ORDER`` keeps the
    public surface narrow without a new error code purely for
    "the offloader sent garbage in its base64 field."
    """
    try:
        return base64.b64decode(b64_text, validate=True)
    except (ValueError, TypeError) as err:
        raise BundleAssemblerError(
            BundleAssemblerErrorCode.OUT_OF_ORDER,
            f"chunk data_b64 failed to decode: {err}",
        ) from err


def compute_bundle_sha256(data: bytes) -> str:
    """Return the lowercase hex SHA-256 digest of *data* for the header."""
    return hashlib.sha256(data).hexdigest()


class BundleAssembler:
    """Reassemble payload bytes from a stream of in-order chunks.

    Driven by both:

    * **Receiver-side bundle assembly** (5c-2 — bundle upload) —
      after the receiver's dispatch parses a ``submit_job``
      header, it constructs one of these against
      :data:`BUNDLE_MAX_TOTAL_BYTES` (the default) and feeds
      each inbound ``submit_job_chunk``.
    * **Offloader-side artifacts assembly** (6a — flash-artifact
      download) — after the offloader receives an
      ``artifacts_start`` header, it constructs one against
      :data:`FIRMWARE_MAX_TOTAL_BYTES` (passed as
      ``max_total_bytes`` kwarg) and feeds each inbound
      ``artifacts_chunk``.

    Same wire shape, same validation rules, same finalise
    semantics — only the size cap differs between the two
    callers. The header values (``total_bytes``,
    ``num_chunks``, ``sha256_hex``) are passed to ``__init__``
    so every subsequent :meth:`feed` call can validate against
    a captured baseline rather than re-reading them from each
    chunk's envelope.

    Lifecycle:

    1. Construct with the header values (``total_bytes``,
       ``num_chunks``, ``sha256_hex``); optionally override
       ``max_total_bytes``.
    2. Call :meth:`feed` once per inbound chunk, in order, with
       its ``chunk_index``, decoded raw bytes, and ``is_last``
       flag. Mismatches raise :class:`BundleAssemblerError`.
    3. After the chunk with ``is_last=True`` lands, call
       :meth:`finalise` to validate the assembled stream
       against the header and return the bytes.

    Re-feeding after :meth:`finalise` succeeds raises
    ``POST_COMPLETION``. The instance is single-use; spin a
    fresh assembler per inbound stream.
    """

    def __init__(
        self,
        *,
        total_bytes: int,
        num_chunks: int,
        sha256_hex: str,
        max_total_bytes: int = BUNDLE_MAX_TOTAL_BYTES,
    ) -> None:
        if total_bytes <= 0:
            _fail(_Code.EMPTY_BUNDLE, f"total_bytes must be positive; got {total_bytes}")
        if num_chunks <= 0:
            _fail(_Code.CHUNK_COUNT_MISMATCH, f"num_chunks must be positive; got {num_chunks}")
        if total_bytes > max_total_bytes:
            _fail(
                _Code.OVERSIZED,
                f"announced total_bytes {total_bytes} exceeds max_total_bytes {max_total_bytes}",
            )
        self._total_bytes = total_bytes
        self._num_chunks = num_chunks
        self._sha256_hex = sha256_hex.lower()
        self._buf = bytearray()
        self._next_index = 0
        self._closed = False

    def feed(self, chunk_index: int, raw: bytes, *, is_last: bool) -> None:
        """Accept one chunk. Raises :class:`BundleAssemblerError` on mismatch."""
        if self._closed:
            _fail(_Code.POST_COMPLETION, f"chunk {chunk_index} arrived after completion")
        if chunk_index != self._next_index:
            _fail(
                _Code.OUT_OF_ORDER,
                f"expected chunk_index {self._next_index}; got {chunk_index}",
            )
        new_total = len(self._buf) + len(raw)
        if new_total > self._total_bytes:
            _fail(
                _Code.OVERSIZED,
                f"chunk {chunk_index} would push assembled bytes to {new_total}, "
                f"exceeding announced total_bytes {self._total_bytes}",
            )
        self._buf.extend(raw)
        self._next_index += 1
        # ``is_last`` must equal "this is the announced final chunk"; anything
        # else is the offloader's chunk-count math drifting from ours. Fail
        # loudly so the misbehaviour surfaces at the wire layer rather than as
        # a corrupt extract.
        is_announced_final = self._next_index == self._num_chunks
        if is_last != is_announced_final:
            _fail(
                _Code.CHUNK_COUNT_MISMATCH,
                f"is_last={is_last} on chunk {chunk_index} mismatches announced "
                f"num_chunks={self._num_chunks} ({self._next_index} arrived)",
            )
        if is_last:
            self._closed = True

    def finalise(self) -> bytes:
        """Validate the assembled stream against the header and return it.

        Caller invokes after the chunk with ``is_last=True``
        has been fed. Raises :class:`BundleAssemblerError` with
        :attr:`BundleAssemblerErrorCode.UNDERSIZED` if the
        assembler hasn't seen its announced last chunk, or
        :attr:`BundleAssemblerErrorCode.HASH_MISMATCH` if the
        SHA-256 of the assembled bytes doesn't match the
        offloader's announced digest.
        """
        if not self._closed:
            _fail(
                _Code.UNDERSIZED,
                f"finalise() called before is_last; "
                f"{self._next_index} of {self._num_chunks} chunks seen",
            )
        if len(self._buf) != self._total_bytes:
            _fail(
                _Code.UNDERSIZED,
                f"assembled {len(self._buf)} bytes, header announced {self._total_bytes}",
            )
        actual = hashlib.sha256(self._buf).hexdigest()
        if actual != self._sha256_hex:
            _fail(
                _Code.HASH_MISMATCH,
                f"assembled bundle sha256 {actual} != announced {self._sha256_hex}",
            )
        return bytes(self._buf)
