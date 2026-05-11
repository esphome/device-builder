"""
Canonical StorageJSON / idedata path resolution for the dashboard.

Single source of truth for "given a :attr:`FirmwareJob.configuration`,
where does its :class:`StorageJSON` sidecar / ``idedata.json`` live
on disk?". Every caller in this project that wants to load a sidecar
should route through :func:`resolve_storage_path` (or the lower-level
:func:`resolve_data_dir` when it needs to compose its own subpath)
rather than calling upstream :func:`esphome.storage_json.ext_storage_path`
directly.

Why a project-local helper rather than just using ``ext_storage_path``:

The upstream helper assumes ``CORE.data_dir`` is the right base for
every YAML — which is true for the user's local YAMLs (the dashboard
process sets ``CORE.config_path`` to a sentinel inside ``config_dir``
on startup and ``data_dir`` resolves off that) but **wrong** for the
receiver-side remote-build flow. There the compile subprocess runs
with ``ESPHOME_DATA_DIR`` pinned to a per-build subtree
(``<config_dir>/.esphome/.remote_builds/<dashboard_id>/<device>/``,
the same path the writer-side env override produces — see
:meth:`FirmwareController._compose_subprocess_env`) so esphome writes
storage / idedata / build under that one
``(dashboard_id, device)``-keyed directory. The dashboard process's
``CORE.data_dir`` still points at the dashboard-wide ``.esphome``
tree, so a naive ``ext_storage_path(configuration)`` call resolves
to a path the subprocess didn't write to — silent
``FileNotFoundError`` on every receiver-side ``download_artifacts``
request after a successful compile.

The fork happens once, here, keyed on whether the configuration
parses through :func:`helpers.remote_build_layout.parse_from_configuration`
as a remote-build path. Local-only callers see identical behaviour
to the old direct-``ext_storage_path`` form because the parser returns
``None`` for bare-basename inputs.

Why a separate module rather than co-locating with
:mod:`helpers.build_artifacts`: ``build_artifacts`` is the
flash-image discovery + idedata-manifest loader; the storage-path
resolution is a more general primitive that several unrelated
call sites need (``config_hash`` for the build-info hash,
``build_size`` for compile size accounting, ``devices.helpers``
for delete / archive sweeps, ``firmware.get_binaries`` for the
download endpoint, …). Keeping the helper here means those callers
don't pull the artifacts-loader's transitive dependencies.
"""

from __future__ import annotations

from pathlib import Path

from esphome.core import CORE

from .remote_build_layout import parse_from_configuration as parse_remote_build_path


def resolve_data_dir(configuration: str) -> Path:
    """Return the ``CORE.data_dir`` the compile of *configuration* wrote into.

    For a receiver-side remote-build job (configuration parses
    as a :class:`RemoteBuildPath`) the compile subprocess runs
    with ``ESPHOME_DATA_DIR`` pinned to the per-build subtree
    (see :meth:`FirmwareController._compose_subprocess_env`),
    so storage / idedata / build_path all land under
    ``<config_dir>/.esphome/.remote_builds/<dashboard_id>/<device>/``
    regardless of the dashboard process's own
    ``CORE.data_dir``. This helper returns that per-build
    directory so the reader looks where the writer landed; for
    everything else (a locally-submitted job, an offloader-side
    handle) it falls back to ``CORE.data_dir`` which honours
    the existing deployment-mode logic (default / HA-addon /
    ``ESPHOME_DATA_DIR`` env override).

    The writer-side env override and this reader-side resolver
    both route the same configuration string through
    :func:`parse_from_configuration` so they agree on the
    directory without an explicit handshake on the wire.
    """
    remote_build_path = parse_remote_build_path(configuration)
    if remote_build_path is not None:
        return remote_build_path.data_dir(Path(CORE.config_path).parent)
    return Path(CORE.data_dir)


def resolve_storage_path(configuration: str) -> Path:
    """Return the :class:`StorageJSON` sidecar path for *configuration*.

    Canonical replacement for direct
    :func:`esphome.storage_json.ext_storage_path` calls in this
    project. Routes through :func:`resolve_data_dir`:

    * **Receiver-side remote-build configurations**
      (``.esphome/.remote_builds/<dashboard_id>/<device>/<device>.yaml``):
      sidecar lives at
      ``<subtree>/storage/<basename>.json``.
    * **Everything else** (the user's local YAMLs, archive /
      delete / get_binaries / get_compiled_device_info, etc.):
      ``<CORE.data_dir>/storage/<basename>.json`` — matches
      upstream :func:`ext_storage_path`'s behaviour.

    Centralised so the writer-side env override (pinned in
    :meth:`FirmwareController._compose_subprocess_env`) and
    every reader-side path resolution share one source of
    truth. Without this seam every consumer would have to fork
    on configuration shape — and the cost of a single caller
    forgetting is a silent ``build_dir_missing`` reject (the
    7a-5 user report). Local-only callers see identical
    behaviour to the old ``ext_storage_path`` form because
    :func:`parse_from_configuration` returns ``None`` for
    bare-basename inputs.

    The keyspace is always the YAML's basename — esphome's
    :func:`esphome.storage_json.storage_path` keys on
    ``CORE.config_filename`` (which is ``Path(config_path).name``)
    so a remote-build path like
    ``.esphome/.remote_builds/<id>/kitchen/kitchen.yaml`` lands
    its sidecar at ``<subtree>/storage/kitchen.yaml.json``, not
    ``<subtree>/storage/.esphome/.remote_builds/...yaml.json``.
    """
    return resolve_data_dir(configuration) / "storage" / f"{Path(configuration).name}.json"


def resolve_idedata_path(configuration: str, *, name: str) -> Path:
    """Return the cached ``idedata/<name>.json`` path for *configuration*.

    Mirror of :func:`esphome.platformio_api._load_idedata`'s
    resolution: ``<CORE.data_dir>/idedata/<name>.json``,
    forked the same way :func:`resolve_storage_path` is so
    receiver-side remote-build reads land under the per-build
    subtree.

    *name* is ``StorageJSON.name`` (the device name esphome
    derived from the YAML's ``esphome:`` block) — passed
    explicitly rather than re-derived from *configuration*
    because the storage sidecar carries the canonical value
    and the YAML's filename may not match it (e.g. user
    renamed the YAML after compile).
    """
    return resolve_data_dir(configuration) / "idedata" / f"{name}.json"
