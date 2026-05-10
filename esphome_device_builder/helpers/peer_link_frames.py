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
implementation. Built on :mod:`voluptuous` (already in the
dependency closure via ESPHome) so per-frame schemas compose
the same way every other input-validation surface in the
project does (see :mod:`helpers.voluptuous_validators` for
the shared chain elements).
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from .voluptuous_validators import not_bool


def frame_schema(required: dict[str, Any]) -> vol.Schema:
    """Build a peer-link frame schema from a *required* field map.

    *required* maps each required field name to its expected
    type or a :mod:`voluptuous` validator. The returned
    :class:`vol.Schema`:

    * Accepts dicts with the listed fields at the expected
      types (``vol.ALLOW_EXTRA`` so optional fields like
      ``reason`` on ``submit_job_ack`` / ``artifacts_end``
      don't trip the schema — callers check those
      individually after the required gate).
    * Wraps every ``int`` type with :func:`not_bool` so a
      stray ``True`` / ``False`` doesn't slip past as a
      valid integer (Python's ``isinstance(True, int)`` is
      true).
    * Wraps every other type as ``vol.Coerce(type)``-less —
      we want strict type matches without silent
      type-coercion, so plain type-as-validator (which
      voluptuous treats as ``isinstance``) is right.

    Pre-compile each frame's schema once at module load
    rather than on every inbound frame; the dispatch loop's
    per-frame cost stays at one schema call.
    """
    cooked: dict[str, Any] = {}
    for field_name, expected in required.items():
        if expected is int:
            cooked[field_name] = vol.All(not_bool, int)
        else:
            cooked[field_name] = expected
    return vol.Schema(cooked, extra=vol.ALLOW_EXTRA, required=True)


def is_valid_frame(schema: vol.Schema, frame: dict[str, Any]) -> bool:
    """Return ``True`` iff *frame* passes *schema*; swallow ``vol.Invalid``.

    Thin wrapper that turns voluptuous's raise-on-mismatch
    contract into a bool return so the dispatch hot path
    can branch with ``if not is_valid_frame(...): drop`` and
    log without unwinding through an exception handler. The
    schema's own error messages aren't surfaced — the
    dispatch sites already do their own debug logging with
    the raw frame, which is more useful than the
    field-by-field voluptuous report for triaging a
    misbehaving peer.
    """
    try:
        schema(frame)
    except vol.Invalid:
        return False
    return True
