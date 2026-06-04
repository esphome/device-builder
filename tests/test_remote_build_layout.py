"""Tests for the layout helper that locks the on-disk shape.

:mod:`helpers.remote_build_layout` is the single source of truth
for the receiver-side remote-build path layout. Round-trip
coverage: every test that exercises a forward construction also
exercises the reverse parse on the same key, so a regression in
either direction trips here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from esphome_device_builder.helpers.remote_build_layout import (
    BUNDLE_SUFFIX,
    REMOTE_BUILDS_SUBDIR,
    RemoteBuildPath,
    parse_from_configuration,
)


def test_subtree_builds_dashboard_device_path(tmp_path: Path) -> None:
    """Subtree path is ``<config>/.esphome/.remote_builds/<dashboard>/<device>/``."""
    key = RemoteBuildPath(dashboard_id="alpha", device_name="kitchen")
    assert key.subtree(tmp_path) == (tmp_path / ".esphome" / ".remote_builds" / "alpha" / "kitchen")


def test_bundle_sits_as_sibling_of_subtree(tmp_path: Path) -> None:
    """Bundle is ``<config>/.esphome/.remote_builds/<dashboard>/<device>.tar.gz``.

    Sibling-not-child is load-bearing: upstream
    :func:`prepare_bundle_for_compile` wipes target_dir before
    extract_bundle reads from bundle_path, so a bundle inside
    target_dir would be deleted mid-flow (PR #552 fix).
    """
    key = RemoteBuildPath(dashboard_id="alpha", device_name="kitchen")
    bundle = key.bundle(tmp_path)
    assert bundle == (tmp_path / ".esphome" / ".remote_builds" / "alpha" / "kitchen.tar.gz")
    assert bundle.parent == key.subtree(tmp_path).parent
    # Use ``str.endswith`` rather than ``.suffix`` so the
    # ``.tar.gz`` compound extension matches; ``.suffix`` only
    # returns the trailing segment (``.gz``).
    assert bundle.name.endswith(BUNDLE_SUFFIX)


def test_remote_builds_subdir_is_dot_prefixed() -> None:
    """Pin the on-disk root location.

    Hidden under ``.esphome/.remote_builds`` so a casual ``ls``
    of the user's config doesn't show it. Pinning the literal
    value here means a future rename ripples through every
    consumer instead of silently breaking the parse helper.
    """
    assert Path(".esphome") / ".remote_builds" == REMOTE_BUILDS_SUBDIR


def test_parse_recovers_key_from_canonical_configuration_path() -> None:
    """A canonical configuration path round-trips through the layout helper.

    :attr:`FirmwareJob.configuration` carries the relative POSIX
    path the writer emits; the reverse-parse recovers the
    ``(dashboard_id, device_name)`` key for the cleanup sweep's
    in-flight gate without each call site re-implementing the
    `parts[...]` chain.
    """
    parsed = parse_from_configuration(".esphome/.remote_builds/alpha/kitchen/kitchen.yaml")
    assert parsed == RemoteBuildPath(dashboard_id="alpha", device_name="kitchen")


def test_parse_handles_deep_nested_yaml_under_subtree() -> None:
    """A configuration deeper than the canonical 5 segments still parses.

    The bundle layout only constrains the first four segments
    (``.esphome / .remote_builds / <dir_id> / <device_name>``);
    the YAML name inside the subtree is free-form. A nested
    YAML (theoretical, not the writer's current shape but
    allowed by the contract) should still resolve.
    """
    parsed = parse_from_configuration(".esphome/.remote_builds/alpha/kitchen/subdir/kitchen.yaml")
    assert parsed == RemoteBuildPath(dashboard_id="alpha", device_name="kitchen")


@pytest.mark.parametrize(
    "configuration",
    [
        # Locally-submitted job at the config-dir root.
        "kitchen.yaml",
        # Under a non-remote-builds subdirectory.
        "subdir/kitchen.yaml",
        # Missing the ``.remote_builds`` segment.
        ".esphome/build/alpha/kitchen/kitchen.yaml",
        # Only the dashboard_id segment, no device_name.
        ".esphome/.remote_builds/alpha/kitchen.yaml",
        # Exactly four segments — no YAML name after device_name.
        ".esphome/.remote_builds/alpha/kitchen",
        # Empty string.
        "",
    ],
)
def test_parse_returns_none_on_non_remote_build_paths(configuration: str) -> None:
    """Configurations that don't match the layout return ``None``.

    Callers that read this value treat ``None`` as "not a
    remote-build job" and skip whatever scan they're running.
    Locally-submitted jobs are the common case; the rest are
    defense-in-depth against hand-edited or malformed paths.
    """
    assert parse_from_configuration(configuration) is None


def test_remote_build_path_is_hashable() -> None:
    """``RemoteBuildPath`` lives in a :class:`frozenset` for the in-flight gate.

    The 6c sweep passes a frozenset of in-flight keys across
    the executor boundary; the dataclass is ``frozen=True`` so
    hashing + immutability hold without manual ``__hash__`` /
    ``__eq__`` impls.
    """
    a = RemoteBuildPath(dashboard_id="alpha", device_name="kitchen")
    b = RemoteBuildPath(dashboard_id="alpha", device_name="kitchen")
    keys = frozenset({a, b})
    assert keys == frozenset({a})
    assert hash(a) == hash(b)


# A 32-char base64url id like older installs persist.
_LONG_ID = "Nc7uJKFUh3U0o6DKioxJFsfpZwWoN5Ws"


def test_long_dashboard_id_renders_8_char_dir(tmp_path: Path) -> None:
    """A >8-char id renders an 8-char on-disk dir, keeping paths under Windows MAX_PATH."""
    key = RemoteBuildPath(dashboard_id=_LONG_ID, device_name="kitchen")
    assert key.dir_id == "Nc7uJKFU"
    assert key.subtree(tmp_path) == (
        tmp_path / ".esphome" / ".remote_builds" / "Nc7uJKFU" / "kitchen"
    )
    assert key.data_dir(tmp_path) == (tmp_path / ".remote_builds" / "Nc7uJKFU" / ".esphome")


def test_parse_round_trip_idempotent_for_long_id(tmp_path: Path) -> None:
    """Parsing a long-id subtree path recovers the 8-char dir_id and renders identical paths."""
    key = RemoteBuildPath(dashboard_id=_LONG_ID, device_name="kitchen")
    configuration = (key.subtree(tmp_path) / "kitchen.yaml").relative_to(tmp_path).as_posix()
    parsed = parse_from_configuration(configuration)
    assert parsed is not None
    assert parsed.dashboard_id == "Nc7uJKFU"  # the truncated dir_id, not the full wire id
    assert parsed.subtree(tmp_path) == key.subtree(tmp_path)
    assert parsed.data_dir(tmp_path) == key.data_dir(tmp_path)
