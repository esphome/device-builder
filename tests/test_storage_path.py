"""Tests for :mod:`helpers.storage_path`.

The canonical storage / idedata path resolver every project-internal
caller routes through. Local-only callers get the same behaviour as
upstream :func:`esphome.storage_json.ext_storage_path`; receiver-side
remote-build configurations land under the per-build subtree the
compile subprocess writes into.
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


def test_resolve_data_dir_remote_build_configuration_uses_per_build_subtree(
    tmp_path: Path,
) -> None:
    """A remote-build configuration resolves to its per-build subtree.

    The receiver-side compile subprocess for that configuration
    runs with ``ESPHOME_DATA_DIR`` pinned to the same subtree
    (see :meth:`FirmwareController._compose_subprocess_env`), so
    the read path here lands where the write path landed.
    """
    configuration = ".esphome/.remote_builds/dashboard-alpha/kitchen/kitchen.yaml"
    assert resolve_data_dir(configuration) == (
        tmp_path / ".esphome" / ".remote_builds" / "dashboard-alpha" / "kitchen"
    )


def test_resolve_data_dir_malformed_remote_build_path_falls_through_to_local() -> None:
    """A configuration that doesn't parse as a remote-build path stays local.

    ``parse_from_configuration`` returns ``None`` for any path
    that doesn't match the canonical
    ``.esphome/.remote_builds/<dashboard_id>/<device>/<file>`` shape.
    A 3-segment shorthand like
    ``.esphome/.remote_builds/<id>/kitchen.yaml`` (no device
    subtree) doesn't qualify and the resolver falls through to
    ``CORE.data_dir`` — same as a bare basename.
    """
    configuration = ".esphome/.remote_builds/dashboard-alpha/kitchen.yaml"
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
    ``<subtree>/kitchen.yaml`` writes its sidecar at
    ``<subtree>/storage/kitchen.yaml.json``, not at
    ``<subtree>/storage/.esphome/.remote_builds/...yaml.json``.
    Pins the basename-keyed contract so a future refactor can't
    silently regress the resolver into emitting the buggy
    full-path key.
    """
    configuration = ".esphome/.remote_builds/dashboard-alpha/kitchen/kitchen.yaml"
    assert resolve_storage_path(configuration) == (
        tmp_path
        / ".esphome"
        / ".remote_builds"
        / "dashboard-alpha"
        / "kitchen"
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
    configuration = ".esphome/.remote_builds/dashboard-alpha/kitchen/kitchen.yaml"
    assert resolve_idedata_path(configuration, name="kitchen") == (
        tmp_path
        / ".esphome"
        / ".remote_builds"
        / "dashboard-alpha"
        / "kitchen"
        / "idedata"
        / "kitchen.json"
    )
