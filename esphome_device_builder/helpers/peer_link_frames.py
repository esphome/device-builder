"""
Wire-frame shape validation shared between peer-link sender / receiver paths.

Defensive runtime check on a peer-controlled dict — Noise AEAD
guarantees the bytes haven't been tampered with in flight, but
the JSON inside is whatever the peer chose to encode and may
not match the TypedDict contract. Indexing missing or
wrong-typed fields would otherwise raise inside the dispatch
hot path and unwind out of the receive loop without an ack /
without firing the corresponding bus event.

The check lives in :mod:`helpers` rather than on either side's
controller so the receiver-side accept handlers
(:mod:`controllers.remote_build.submit_job`) and the
offloader-side receive loop
(:mod:`controllers.remote_build.peer_link_client`) share one
implementation. Optional fields are deliberately out of scope —
callers that need them validate post-required-pass on the
specific field.
"""

from __future__ import annotations

from typing import Any


def validate_frame_shape(frame: dict[str, Any], required: dict[str, type]) -> bool:
    """Return ``True`` iff *frame* has every *required* field at the matching type.

    *required* is a mapping of field name → expected type. ``bool``
    is special-cased because it's a subclass of ``int`` in Python —
    a frame announcing ``total_bundle_bytes=True`` would otherwise
    pass the ``int`` check. Bool is accepted only when the contract
    explicitly asks for ``bool``.

    Optional fields belong outside this gate: the function returns
    ``True`` as soon as every required field passes, regardless of
    extra keys. Callers that consume an optional field
    (e.g. ``SubmitJobAckFrameData.reason``) check it on the
    specific value after the required-shape check.
    """
    for field_name, expected in required.items():
        value = frame.get(field_name)
        if not isinstance(value, expected):
            return False
        if expected is int and isinstance(value, bool):
            return False
    return True
