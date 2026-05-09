"""
Tests for the project-local :mod:`voluptuous` validators.

Pin the contract of each custom validator so a future
"refactor a chain to use a different upstream primitive"
doesn't silently change the rejection surface.
"""

from __future__ import annotations

import pytest
import voluptuous as vol

from esphome_device_builder.helpers.voluptuous_validators import lowercase_hex, not_bool


def test_not_bool_rejects_true() -> None:
    with pytest.raises(vol.Invalid, match="must not be a bool"):
        not_bool(True)


def test_not_bool_rejects_false() -> None:
    with pytest.raises(vol.Invalid, match="must not be a bool"):
        not_bool(False)


def test_not_bool_passes_int() -> None:
    assert not_bool(0) == 0
    assert not_bool(1) == 1
    assert not_bool(-42) == -42


def test_not_bool_passes_str() -> None:
    """Non-bool, non-int values flow through; downstream chain decides."""
    assert not_bool("hi") == "hi"


def test_not_bool_passes_none() -> None:
    assert not_bool(None) is None


def test_not_bool_chains_with_int_range() -> None:
    """Real usage shape: port validator rejecting ``True`` / ``False`` first.

    Without :func:`not_bool` the chain would silently accept
    ``True`` as port 1 (Python's ``isinstance(True, int)`` is
    true, so ``vol.Range`` accepts it).
    """
    schema = vol.Schema(vol.All(not_bool, int, vol.Range(min=1, max=65535)))

    with pytest.raises(vol.Invalid, match="must not be a bool"):
        schema(True)
    with pytest.raises(vol.Invalid, match="must not be a bool"):
        schema(False)
    assert schema(6055) == 6055
    with pytest.raises(vol.Invalid):
        schema(70000)


def test_lowercase_hex_accepts_canonical_hash() -> None:
    schema = vol.Schema(lowercase_hex(64))
    assert schema("a" * 64) == "a" * 64
    assert schema("0123456789abcdef" * 4) == "0123456789abcdef" * 4


def test_lowercase_hex_rejects_uppercase() -> None:
    schema = vol.Schema(lowercase_hex(64))
    with pytest.raises(vol.Invalid):
        schema("A" * 64)
    with pytest.raises(vol.Invalid):
        schema("0123456789ABCDEF" * 4)


def test_lowercase_hex_rejects_non_hex_alphabet() -> None:
    """Right length, wrong alphabet (``z`` is outside [0-9a-f])."""
    schema = vol.Schema(lowercase_hex(64))
    with pytest.raises(vol.Invalid):
        schema("z" * 64)


def test_lowercase_hex_rejects_wrong_length() -> None:
    schema = vol.Schema(lowercase_hex(64))
    with pytest.raises(vol.Invalid):
        schema("a" * 63)
    with pytest.raises(vol.Invalid):
        schema("a" * 65)


def test_lowercase_hex_rejects_non_string() -> None:
    schema = vol.Schema(lowercase_hex(64))
    with pytest.raises(vol.Invalid):
        schema(b"a" * 64)


def test_lowercase_hex_factory_parametric() -> None:
    """The length is parametric so callers can validate other digest sizes."""
    schema_8 = vol.Schema(lowercase_hex(8))  # config_hash shape
    assert schema_8("deadbeef") == "deadbeef"
    with pytest.raises(vol.Invalid):
        schema_8("deadbeef0")  # too long
    with pytest.raises(vol.Invalid):
        schema_8("DEADBEEF")  # uppercase
