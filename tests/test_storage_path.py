"""Tests for :mod:`helpers.storage_path`.

The canonical storage / idedata path resolver every project-internal
caller routes through. Local-only callers get the same behaviour as
upstream :func:`esphome.storage_json.ext_storage_path`; receiver-side
remote-build configurations land under a per-dashboard subdirectory
of ``CORE.data_dir`` that the compile subprocess writes into.
"""

from __future__ import annotations

from pathlib import Path

from esphome.core import CORE

from esphome_device_builder.helpers.storage_path import (
    resolve_data_dir,
    resolve_idedata_path,
    resolve_storage_path,
)


def test_resolve_data_dir_local_configuration_uses_core_data_dir() -> None:
    """A bare-basename configuration resolves to ``CORE.data_dir``.

    The dashboard process owns ``CORE.config_path`` (pinned to a
    sentinel under ``config_dir`` on startup) and therefore
    ``CORE.data_dir``; falling through to that path on a
    locally-submitted job matches upstream :func:`ext_storage_path`.
    """
    assert resolve_data_dir("kitchen.yaml") == Path(CORE.data_dir)


def test_resolve_data_dir_remote_build_configuration_uses_per_dashboard_esphome(
    tmp_path: Path,
) -> None:
    """A remote-build configuration resolves to its per-dashboard ``.esphome``.

    The receiver-side compile subprocess for that configuration
    runs with ``ESPHOME_DATA_DIR`` pinned to the same per-dashboard
    directory anchored on ``CORE.data_dir`` (see
    :meth:`FirmwareController._compose_subprocess_env`), so the
    read path here lands where the write path landed. In default
    test mode ``CORE.data_dir`` is ``<tmp_path>/.esphome`` so the
    resolved value is equivalent to the old per-build-subtree
    location for the same configuration — but the resolver is
    anchored on ``CORE.data_dir`` to keep HA-addon builds out of
    the ``/config`` volume.
    """
    configuration = ".esphome/.remote_builds/a1b2c3d4/kitchen/kitchen.yaml"
    assert resolve_data_dir(configuration) == (
        tmp_path / ".esphome" / ".remote_builds" / "a1b2c3d4" / ".esphome"
    )


def test_resolve_data_dir_malformed_remote_build_path_falls_through_to_local() -> None:
    """A configuration that doesn't parse as a remote-build path stays local.

    ``parse_from_configuration`` returns ``None`` for any path
    that doesn't match the canonical
    ``.esphome/.remote_builds/<dir_id>/<device>/<file>`` shape.
    A 3-segment shorthand like
    ``.esphome/.remote_builds/<dir_id>/kitchen.yaml`` (no device
    subtree) doesn't qualify and the resolver falls through to
    ``CORE.data_dir`` — same as a bare basename.
    """
    configuration = ".esphome/.remote_builds/a1b2c3d4/kitchen.yaml"
    assert resolve_data_dir(configuration) == Path(CORE.data_dir)


def test_resolve_storage_path_local_configuration() -> None:
    """Storage sidecar for a local YAML lives under ``<CORE.data_dir>/storage/<basename>.json``."""
    assert resolve_storage_path("kitchen.yaml") == (
        Path(CORE.data_dir) / "storage" / "kitchen.yaml.json"
    )


def test_resolve_storage_path_remote_build_uses_basename_keyspace(tmp_path: Path) -> None:
    """Remote-build sidecar is keyed on the YAML basename, not the full configuration.

    Mirrors esphome's :func:`storage_path` which keys on
    ``CORE.config_filename`` (the basename of
    ``CORE.config_path``) — a remote-build YAML at
    ``<per-dashboard-data-dir>/kitchen.yaml`` writes its sidecar
    at ``<per-dashboard-data-dir>/storage/kitchen.yaml.json``,
    where ``<per-dashboard-data-dir>`` is
    ``<CORE.data_dir>/.remote_builds/<dir_id>/.esphome``.
    The full configuration string (``.esphome/.remote_builds/<dir_id>/<device>/<device>.yaml``)
    is not re-embedded in the sidecar path — pins the
    basename-keyed contract so a future refactor can't silently
    regress the resolver into emitting the buggy full-path key.
    """
    configuration = ".esphome/.remote_builds/a1b2c3d4/kitchen/kitchen.yaml"
    assert resolve_storage_path(configuration) == (
        tmp_path
        / ".esphome"
        / ".remote_builds"
        / "a1b2c3d4"
        / ".esphome"
        / "storage"
        / "kitchen.yaml.json"
    )


def test_resolve_idedata_path_local_configuration() -> None:
    """Idedata cache for a local YAML lives under ``<CORE.data_dir>/idedata/<name>.json``."""
    assert resolve_idedata_path("kitchen.yaml", name="kitchen") == (
        Path(CORE.data_dir) / "idedata" / "kitchen.json"
    )


def test_resolve_idedata_path_remote_build(tmp_path: Path) -> None:
    """Idedata for a remote build lands under the per-build subtree."""
    configuration = ".esphome/.remote_builds/a1b2c3d4/kitchen/kitchen.yaml"
    assert resolve_idedata_path(configuration, name="kitchen") == (
        tmp_path
        / ".esphome"
        / ".remote_builds"
        / "a1b2c3d4"
        / ".esphome"
        / "idedata"
        / "kitchen.json"
    )
