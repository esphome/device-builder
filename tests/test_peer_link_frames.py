"""Unit tests for :mod:`helpers.peer_link_frames`."""

from __future__ import annotations

from esphome_device_builder.helpers.peer_link_frames import (
    frame_schema,
    is_valid_frame,
    safe_job_id,
)


def test_is_valid_frame_accepts_matching_required_fields() -> None:
    """A frame with every required field at the right type passes."""
    schema = frame_schema({"job_id": str, "accepted": bool})
    frame = {"job_id": "abc", "accepted": True, "extra": "ignored"}
    assert is_valid_frame(schema, frame) is True


def test_is_valid_frame_rejects_missing_field() -> None:
    """A frame missing a required field is rejected."""
    schema = frame_schema({"job_id": str, "accepted": bool})
    frame = {"job_id": "abc"}
    assert is_valid_frame(schema, frame) is False


def test_is_valid_frame_rejects_wrong_type() -> None:
    """A frame with a wrong-typed required field is rejected."""
    schema = frame_schema({"job_id": str, "accepted": bool})
    frame = {"job_id": 42, "accepted": True}
    assert is_valid_frame(schema, frame) is False


def test_is_valid_frame_rejects_bool_for_int_field() -> None:
    """``int`` field rejects ``bool`` even though ``bool`` is a subclass of ``int``."""
    schema = frame_schema({"count": int})
    frame = {"count": True}
    assert is_valid_frame(schema, frame) is False


def test_is_valid_frame_accepts_int_for_int_field() -> None:
    """A real ``int`` passes the int gate."""
    schema = frame_schema({"count": int})
    frame = {"count": 7}
    assert is_valid_frame(schema, frame) is True


def test_is_valid_frame_accepts_bool_for_bool_field() -> None:
    """A ``bool`` field accepts a ``bool`` value."""
    schema = frame_schema({"flag": bool})
    frame = {"flag": True}
    assert is_valid_frame(schema, frame) is True


def test_is_valid_frame_empty_required_passes_any_frame() -> None:
    """An empty *required* mapping vacuously passes any frame."""
    schema = frame_schema({})
    assert is_valid_frame(schema, {}) is True
    assert is_valid_frame(schema, {"x": 1}) is True


def test_safe_job_id_returns_string_job_id() -> None:
    """A string ``job_id`` passes through."""
    assert safe_job_id({"job_id": "j-1"}) == "j-1"


def test_safe_job_id_falls_back_on_missing_key() -> None:
    """A frame with no ``job_id`` yields the empty fallback."""
    assert safe_job_id({}) == ""


def test_safe_job_id_falls_back_on_non_string() -> None:
    """A wrong-typed ``job_id`` yields the empty fallback."""
    assert safe_job_id({"job_id": 42}) == ""
    assert safe_job_id({"job_id": None}) == ""
