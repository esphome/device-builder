"""Tests for the offloader's version-match policy helpers."""

from __future__ import annotations

import pytest

from esphome_device_builder.helpers.version_compat import (
    VersionMatchPolicy,
    major_versions_match,
    version_satisfies_policy,
    versions_match_exactly,
)


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


@pytest.mark.parametrize(
    ("local", "peer", "expected"),
    [
        pytest.param("2026.5.0", "2026.5.0", True, id="exact_match"),
        # Any difference at all fails — patch and release alike.
        pytest.param("2026.5.0", "2026.5.1", False, id="patch_diff"),
        pytest.param("2026.5.0", "2026.6.0", False, id="release_drift"),
        pytest.param("2026.5.0", "2026.5.0-dev", False, id="suffix_diff"),
        # Empty-string carve-out matches ``major_versions_match`` so
        # a fresh APPROVED pairing isn't filtered before its
        # first session-open populates ``esphome_version``.
        pytest.param("", "2026.5.0", True, id="empty_local"),
        pytest.param("2026.5.0", "", True, id="empty_peer"),
        pytest.param("", "", True, id="both_empty"),
    ],
)
def test_versions_match_exactly(local: str, peer: str, expected: bool) -> None:
    """Byte-for-byte equality with the shared empty-string forgiveness."""
    assert versions_match_exactly(local, peer) is expected


@pytest.mark.parametrize(
    ("policy", "local", "peer", "expected"),
    [
        # ANY always passes.
        pytest.param(VersionMatchPolicy.ANY, "2026.5.0", "2026.6.0", True, id="any_release_drift"),
        pytest.param(VersionMatchPolicy.ANY, "2026.5.0", "2026.5.1", True, id="any_patch_drift"),
        # RELEASE delegates to ``major_versions_match``.
        pytest.param(
            VersionMatchPolicy.RELEASE, "2026.5.0", "2026.5.1", True, id="release_patch_ok"
        ),
        pytest.param(
            VersionMatchPolicy.RELEASE, "2026.5.0", "2026.6.0", False, id="release_drift_filtered"
        ),
        # EXACT delegates to ``versions_match_exactly``.
        pytest.param(VersionMatchPolicy.EXACT, "2026.5.0", "2026.5.0", True, id="exact_match"),
        pytest.param(
            VersionMatchPolicy.EXACT, "2026.5.0", "2026.5.1", False, id="exact_patch_filtered"
        ),
        # EXACT_REQUIRED matches EXACT on populated versions but
        # tightens the empty-string slack (no LOCAL fallback).
        pytest.param(
            VersionMatchPolicy.EXACT_REQUIRED,
            "2026.5.0",
            "2026.5.0",
            True,
            id="exact_required_match",
        ),
        pytest.param(
            VersionMatchPolicy.EXACT_REQUIRED,
            "2026.5.0",
            "2026.5.1",
            False,
            id="exact_required_patch_filtered",
        ),
        pytest.param(
            VersionMatchPolicy.EXACT_REQUIRED, "2026.5.0", "", False, id="exact_required_empty_peer"
        ),
        pytest.param(
            VersionMatchPolicy.EXACT_REQUIRED,
            "",
            "2026.5.0",
            False,
            id="exact_required_empty_local",
        ),
    ],
)
def test_version_satisfies_policy(
    policy: VersionMatchPolicy, local: str, peer: str, expected: bool
) -> None:
    """Per-policy dispatch matches the underlying comparator's verdict."""
    assert version_satisfies_policy(local, peer, policy) is expected


def test_version_satisfies_policy_empty_strings_pass_lax_policies() -> None:
    """Lax policies forgive missing version strings; ``EXACT_REQUIRED`` does not.

    The empty-string slack on ``major_versions_match`` /
    ``versions_match_exactly`` exists so a fresh APPROVED pairing
    isn't filtered before its first session-open populates
    ``esphome_version``. ``EXACT_REQUIRED`` opts out: no LOCAL
    safety net means an unknown peer can't be admitted.
    """
    for policy in (VersionMatchPolicy.ANY, VersionMatchPolicy.RELEASE, VersionMatchPolicy.EXACT):
        assert version_satisfies_policy("", "2026.5.0", policy) is True
        assert version_satisfies_policy("2026.5.0", "", policy) is True
    assert version_satisfies_policy("", "2026.5.0", VersionMatchPolicy.EXACT_REQUIRED) is False
    assert version_satisfies_policy("2026.5.0", "", VersionMatchPolicy.EXACT_REQUIRED) is False
