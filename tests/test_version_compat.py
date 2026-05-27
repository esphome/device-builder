"""Tests for the offloader's major-version-match helper."""

from __future__ import annotations

import pytest

from esphome_device_builder.helpers.version_compat import major_versions_match


@pytest.mark.parametrize(
    ("local", "peer", "expected"),
    [
        # Exact matches.
        pytest.param("2026.5.0", "2026.5.0", True, id="exact_match"),
        # Patch-level differences inside the same release line.
        pytest.param("2026.5.0", "2026.5.1", True, id="patch_diff"),
        pytest.param("2026.5.0b1", "2026.5.0", True, id="prerelease_suffix_on_patch"),
        pytest.param("2026.5.0-dev", "2026.5.0", True, id="dev_suffix_on_patch"),
        pytest.param("2026.5b1", "2026.5", True, id="prerelease_suffix_on_month"),
        # Cross-release drift (the case the gate fires on).
        pytest.param("2026.5.0", "2026.6.0", False, id="month_drift"),
        pytest.param("2026.5.0", "2027.5.0", False, id="year_drift"),
        pytest.param("2026.5.0-dev", "2026.6.0", False, id="dev_then_release_drift"),
        pytest.param("2026.4.5", "2026.5.0", False, id="reporter_985_skew"),
        # Empty / unknown — match so a fresh APPROVED pairing
        # isn't filtered before its first peer-link session-open.
        pytest.param("", "2026.5.0", True, id="empty_local"),
        pytest.param("2026.5.0", "", True, id="empty_peer"),
        pytest.param("", "", True, id="both_empty"),
    ],
)
def test_major_versions_match(local: str, peer: str, expected: bool) -> None:
    """Major-version match across the realistic version-string matrix."""
    assert major_versions_match(local, peer) is expected
