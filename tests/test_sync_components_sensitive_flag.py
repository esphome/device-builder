"""Tests for the ``sensitive`` flag handling in ``_convert_field``.

Upstream esphome (esphome/esphome#16673) emits ``"sensitive": true`` on
schema entries whose validator is wrapped in ``cv.sensitive(...)``. Our
sync prefers that explicit signal over the legacy key-name fragment
heuristic, while keeping the heuristic as a fallback for older esphome
versions (and for unmigrated/third-party schemas) that don't carry the
flag yet.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from script.sync_components import (  # type: ignore[import-not-found]
    _convert_field,
)


@pytest.fixture
def schema_dir(tmp_path: Path) -> Path:
    """Empty dir for ``_convert_field`` (only used for ``extends`` lookups)."""
    return tmp_path


def test_explicit_sensitive_flag_promotes_string_to_secure_string(
    schema_dir: Path,
) -> None:
    """Explicit ``"sensitive": true`` flips a string entry to secure_string."""
    raw = {"type": "string", "key": "Optional", "sensitive": True}
    entry = _convert_field("bearer", raw, schema_dir)
    assert entry is not None
    assert entry["type"] == "secure_string"


def test_fragment_heuristic_still_promotes_for_pre_marker_esphome(
    schema_dir: Path,
) -> None:
    """Pre-marker esphome has no flag; the fragment list still masks the field."""
    raw = {"type": "string", "key": "Optional"}
    entry = _convert_field("wifi_password", raw, schema_dir)
    assert entry is not None
    assert entry["type"] == "secure_string"


def test_non_sensitive_string_field_stays_string(schema_dir: Path) -> None:
    """No marker, non-matching name; entry stays as a plain ``string``."""
    raw = {"type": "string", "key": "Optional"}
    entry = _convert_field("hostname", raw, schema_dir)
    assert entry is not None
    assert entry["type"] == "string"


def test_sensitive_flag_on_non_string_type_does_not_promote(
    schema_dir: Path,
) -> None:
    """Promotion is gated on string type; a sensitive boolean stays boolean."""
    raw = {"type": "boolean", "key": "Optional", "sensitive": True}
    entry = _convert_field("encrypted", raw, schema_dir)
    assert entry is not None
    assert entry["type"] == "boolean"
