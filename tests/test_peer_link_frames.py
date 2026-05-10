"""Unit tests for :mod:`helpers.peer_link_frames`."""

from __future__ import annotations

from esphome_device_builder.helpers.peer_link_frames import validate_frame_shape


def test_validate_frame_shape_accepts_matching_required_fields() -> None:
    """A frame with every required field at the right type passes."""
    frame = {"job_id": "abc", "accepted": True, "extra": "ignored"}
    assert validate_frame_shape(frame, {"job_id": str, "accepted": bool}) is True


def test_validate_frame_shape_rejects_missing_field() -> None:
    """A frame missing a required field is rejected."""
    frame = {"job_id": "abc"}
    assert validate_frame_shape(frame, {"job_id": str, "accepted": bool}) is False


def test_validate_frame_shape_rejects_wrong_type() -> None:
    """A frame with a wrong-typed required field is rejected."""
    frame = {"job_id": 42, "accepted": True}
    assert validate_frame_shape(frame, {"job_id": str, "accepted": bool}) is False


def test_validate_frame_shape_rejects_bool_for_int_field() -> None:
    """``int`` field rejects ``bool`` even though ``bool`` is a subclass of ``int``."""
    frame = {"count": True}
    assert validate_frame_shape(frame, {"count": int}) is False


def test_validate_frame_shape_accepts_int_for_int_field() -> None:
    """A real ``int`` passes the int gate."""
    frame = {"count": 7}
    assert validate_frame_shape(frame, {"count": int}) is True


def test_validate_frame_shape_accepts_bool_for_bool_field() -> None:
    """A ``bool`` field accepts a ``bool`` value."""
    frame = {"flag": True}
    assert validate_frame_shape(frame, {"flag": bool}) is True


def test_validate_frame_shape_empty_required_passes_any_frame() -> None:
    """An empty *required* mapping vacuously passes any frame."""
    assert validate_frame_shape({}, {}) is True
    assert validate_frame_shape({"x": 1}, {}) is True
