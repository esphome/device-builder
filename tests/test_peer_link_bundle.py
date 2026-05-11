"""
Tests for the bundle chunking + reassembly helpers from ``peer_link_bundle``.

Shared between the receiver-side dispatch and offloader-side
``submit_job`` flow. Pure helpers; no controller / WS state. The wire
format is exercised end-to-end (chunk → encode → decode → assemble → finalise)
so a contract drift in either direction surfaces here before it lands at the
channel layer.
"""

from __future__ import annotations

import hashlib
import secrets

import pytest

from esphome_device_builder.helpers.peer_link_bundle import (
    BUNDLE_CHUNK_SIZE_BYTES,
    BUNDLE_MAX_TOTAL_BYTES,
    BundleAssembler,
    BundleAssemblerError,
    BundleAssemblerErrorCode,
    chunk_bundle,
    compute_bundle_sha256,
    decode_chunk,
    encode_chunk,
)

# ---------------------------------------------------------------------------
# chunk_bundle
# ---------------------------------------------------------------------------


def test_chunk_bundle_yields_single_full_chunk() -> None:
    """A bundle exactly chunk_size long → one chunk, is_last=True."""
    data = b"x" * 1024
    chunks = list(chunk_bundle(data, chunk_size=1024))
    assert len(chunks) == 1
    assert chunks[0] == (0, b"x" * 1024, True)


def test_chunk_bundle_yields_partial_last_chunk() -> None:
    """A bundle with size > chunk_size that doesn't divide evenly."""
    data = b"a" * 1500
    chunks = list(chunk_bundle(data, chunk_size=1024))
    assert len(chunks) == 2
    assert chunks[0] == (0, b"a" * 1024, False)
    assert chunks[1] == (1, b"a" * 476, True)


def test_chunk_bundle_yields_three_chunks_with_exact_last() -> None:
    """Bundle that divides evenly across three chunks."""
    data = b"q" * 3072
    chunks = list(chunk_bundle(data, chunk_size=1024))
    assert [c[0] for c in chunks] == [0, 1, 2]
    assert [c[2] for c in chunks] == [False, False, True]
    assert b"".join(c[1] for c in chunks) == data


def test_chunk_bundle_empty_yields_nothing() -> None:
    """An empty payload yields no chunks; caller must reject at header layer."""
    assert list(chunk_bundle(b"", chunk_size=1024)) == []


def test_chunk_bundle_rejects_non_positive_chunk_size() -> None:
    """``chunk_size <= 0`` raises immediately, not on the first ``next()``."""
    with pytest.raises(ValueError, match="chunk_size must be positive"):
        chunk_bundle(b"data", chunk_size=0)
    with pytest.raises(ValueError, match="chunk_size must be positive"):
        chunk_bundle(b"data", chunk_size=-5)


def test_chunk_bundle_default_chunk_size() -> None:
    """Default chunk_size is :data:`BUNDLE_CHUNK_SIZE_BYTES`."""
    data = b"y" * (BUNDLE_CHUNK_SIZE_BYTES + 1)
    chunks = list(chunk_bundle(data))
    assert len(chunks) == 2
    assert len(chunks[0][1]) == BUNDLE_CHUNK_SIZE_BYTES
    assert len(chunks[1][1]) == 1


# ---------------------------------------------------------------------------
# encode_chunk / decode_chunk round-trip
# ---------------------------------------------------------------------------


def test_encode_decode_round_trips_random_bytes() -> None:
    """Random binary payload survives base64 round-trip exactly."""
    data = secrets.token_bytes(2048)
    assert decode_chunk(encode_chunk(data)) == data


def test_decode_chunk_rejects_garbage() -> None:
    """Malformed base64 raises ``BundleAssemblerError(OUT_OF_ORDER)``."""
    with pytest.raises(BundleAssemblerError) as excinfo:
        decode_chunk("!@#$ not base64 ###")
    assert excinfo.value.code is BundleAssemblerErrorCode.OUT_OF_ORDER


# ---------------------------------------------------------------------------
# BundleAssembler — happy path
# ---------------------------------------------------------------------------


def _header(data: bytes, *, chunk_size: int = 1024) -> dict:
    """Build the constructor kwargs from a bundle payload."""
    chunks = list(chunk_bundle(data, chunk_size=chunk_size))
    return {
        "total_bytes": len(data),
        "num_chunks": len(chunks),
        "sha256_hex": compute_bundle_sha256(data),
    }


def test_assembler_round_trips_single_chunk_bundle() -> None:
    """A one-chunk bundle assembles + finalises to the original bytes."""
    data = b"hello world"
    asm = BundleAssembler(**_header(data))
    asm.feed(0, data, is_last=True)
    assert asm.finalise() == data


def test_assembler_round_trips_multi_chunk_bundle() -> None:
    """A multi-chunk bundle assembles + finalises to the original bytes."""
    data = secrets.token_bytes(3500)
    asm = BundleAssembler(**_header(data, chunk_size=1024))
    for index, raw, is_last in chunk_bundle(data, chunk_size=1024):
        asm.feed(index, raw, is_last=is_last)
    assert asm.finalise() == data


def test_assembler_handles_default_chunk_size_round_trip() -> None:
    """Production-shaped bundle (~50 KiB at default chunk size) round-trips."""
    data = secrets.token_bytes(50 * 1024)
    asm = BundleAssembler(**_header(data, chunk_size=BUNDLE_CHUNK_SIZE_BYTES))
    for index, raw, is_last in chunk_bundle(data):
        asm.feed(index, raw, is_last=is_last)
    assert asm.finalise() == data


# ---------------------------------------------------------------------------
# BundleAssembler — header validation
# ---------------------------------------------------------------------------


def test_assembler_rejects_non_positive_total_bytes() -> None:
    with pytest.raises(BundleAssemblerError) as excinfo:
        BundleAssembler(total_bytes=0, num_chunks=1, sha256_hex="0" * 64)
    assert excinfo.value.code is BundleAssemblerErrorCode.EMPTY_BUNDLE


def test_assembler_rejects_non_positive_num_chunks() -> None:
    with pytest.raises(BundleAssemblerError) as excinfo:
        BundleAssembler(total_bytes=10, num_chunks=0, sha256_hex="0" * 64)
    assert excinfo.value.code is BundleAssemblerErrorCode.CHUNK_COUNT_MISMATCH


def test_assembler_rejects_oversized_announcement() -> None:
    """A header announcing more than :data:`BUNDLE_MAX_TOTAL_BYTES` is rejected."""
    with pytest.raises(BundleAssemblerError) as excinfo:
        BundleAssembler(
            total_bytes=BUNDLE_MAX_TOTAL_BYTES + 1,
            num_chunks=999,
            sha256_hex="0" * 64,
        )
    assert excinfo.value.code is BundleAssemblerErrorCode.OVERSIZED


# ---------------------------------------------------------------------------
# BundleAssembler — feed-time misbehaviours
# ---------------------------------------------------------------------------


def test_assembler_rejects_out_of_order_chunk() -> None:
    """A chunk with a non-monotonic index raises ``OUT_OF_ORDER``."""
    data = secrets.token_bytes(2048)
    asm = BundleAssembler(**_header(data, chunk_size=1024))
    asm.feed(0, data[:1024], is_last=False)
    with pytest.raises(BundleAssemblerError) as excinfo:
        asm.feed(2, data[1024:], is_last=True)  # skipped index 1
    assert excinfo.value.code is BundleAssemblerErrorCode.OUT_OF_ORDER


def test_assembler_rejects_duplicate_chunk_index() -> None:
    """Replaying the same chunk index → ``OUT_OF_ORDER``."""
    data = secrets.token_bytes(2048)
    asm = BundleAssembler(**_header(data, chunk_size=1024))
    asm.feed(0, data[:1024], is_last=False)
    with pytest.raises(BundleAssemblerError) as excinfo:
        asm.feed(0, data[:1024], is_last=False)
    assert excinfo.value.code is BundleAssemblerErrorCode.OUT_OF_ORDER


def test_assembler_rejects_oversize_aggregate() -> None:
    """A chunk that pushes total past announced ``total_bytes`` is rejected."""
    asm = BundleAssembler(total_bytes=100, num_chunks=2, sha256_hex="0" * 64)
    asm.feed(0, b"x" * 60, is_last=False)
    with pytest.raises(BundleAssemblerError) as excinfo:
        asm.feed(1, b"x" * 50, is_last=True)  # 60 + 50 > 100
    assert excinfo.value.code is BundleAssemblerErrorCode.OVERSIZED


def test_assembler_rejects_post_completion_feed() -> None:
    """Feeding after the last chunk landed → ``POST_COMPLETION``."""
    data = b"abc" * 100
    asm = BundleAssembler(**_header(data, chunk_size=len(data)))
    asm.feed(0, data, is_last=True)
    with pytest.raises(BundleAssemblerError) as excinfo:
        asm.feed(1, b"more", is_last=True)
    assert excinfo.value.code is BundleAssemblerErrorCode.POST_COMPLETION


def test_assembler_rejects_premature_is_last_with_short_count() -> None:
    """``is_last=True`` while announced ``num_chunks`` not yet seen → mismatch."""
    asm = BundleAssembler(total_bytes=2048, num_chunks=2, sha256_hex="0" * 64)
    with pytest.raises(BundleAssemblerError) as excinfo:
        asm.feed(0, b"x" * 1024, is_last=True)
    assert excinfo.value.code is BundleAssemblerErrorCode.CHUNK_COUNT_MISMATCH


def test_assembler_rejects_missing_is_last_on_announced_final() -> None:
    """Final chunk without ``is_last=True`` → ``CHUNK_COUNT_MISMATCH``."""
    asm = BundleAssembler(total_bytes=1024, num_chunks=1, sha256_hex="0" * 64)
    with pytest.raises(BundleAssemblerError) as excinfo:
        asm.feed(0, b"x" * 1024, is_last=False)
    assert excinfo.value.code is BundleAssemblerErrorCode.CHUNK_COUNT_MISMATCH


# ---------------------------------------------------------------------------
# BundleAssembler — finalise
# ---------------------------------------------------------------------------


def test_assembler_finalise_before_is_last_raises() -> None:
    """Calling ``finalise`` before the assembler closed → ``UNDERSIZED``."""
    asm = BundleAssembler(total_bytes=2048, num_chunks=2, sha256_hex="0" * 64)
    asm.feed(0, b"x" * 1024, is_last=False)
    with pytest.raises(BundleAssemblerError) as excinfo:
        asm.finalise()
    assert excinfo.value.code is BundleAssemblerErrorCode.UNDERSIZED


def test_assembler_finalise_with_short_assembled_bytes_raises() -> None:
    """Last chunk landed but bytes short of announced ``total_bytes``.

    Synthesised by lying in the header (announce 1024 bytes,
    feed 512). Real wire-level shape: ``num_chunks`` matches
    but the offloader's chunk-size math drifted from the
    receiver's.
    """
    asm = BundleAssembler(total_bytes=1024, num_chunks=1, sha256_hex="0" * 64)
    asm.feed(0, b"x" * 512, is_last=True)
    with pytest.raises(BundleAssemblerError) as excinfo:
        asm.finalise()
    assert excinfo.value.code is BundleAssemblerErrorCode.UNDERSIZED


def test_assembler_finalise_hash_mismatch() -> None:
    """A bundle whose bytes don't hash to the announced digest is rejected."""
    data = b"actual bundle bytes"
    asm = BundleAssembler(
        total_bytes=len(data),
        num_chunks=1,
        sha256_hex="f" * 64,  # wrong on purpose
    )
    asm.feed(0, data, is_last=True)
    with pytest.raises(BundleAssemblerError) as excinfo:
        asm.finalise()
    assert excinfo.value.code is BundleAssemblerErrorCode.HASH_MISMATCH


def test_assembler_finalise_is_idempotent() -> None:
    """A second ``finalise()`` on a closed assembler returns the same bytes.

    The closed-flag guard lives in :meth:`feed`, not
    :meth:`finalise`; ``finalise`` re-validates the
    aggregate (length + hash) against the stored header and
    returns the buffer regardless of whether it's been called
    before. Pinned to document the actual contract: callers
    don't *need* to memoise the return value, and re-calling
    is a no-op rather than an error. If a future change adds
    a single-use guard (raising ``POST_COMPLETION`` on the
    second call), this test should be flipped to assert that
    raise instead, alongside the production change.
    """
    data = b"hash me"
    asm = BundleAssembler(**_header(data, chunk_size=len(data)))
    asm.feed(0, data, is_last=True)
    assert asm.finalise() == data
    assert asm.finalise() == data


# ---------------------------------------------------------------------------
# Hash helper
# ---------------------------------------------------------------------------


def test_compute_bundle_sha256_matches_hashlib() -> None:
    """Sanity check: helper produces the canonical hex digest shape."""
    data = secrets.token_bytes(4096)
    assert compute_bundle_sha256(data) == hashlib.sha256(data).hexdigest()
    assert compute_bundle_sha256(data) == compute_bundle_sha256(data).lower()
    assert len(compute_bundle_sha256(data)) == 64
