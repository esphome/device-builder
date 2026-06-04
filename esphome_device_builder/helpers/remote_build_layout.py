"""
Single source of truth for the receiver-side remote-build on-disk layout.

Per-device YAML extract lives at

    ``<config_dir>/.esphome/.remote_builds/<dir_id>/<device_name>/``

where ``<dir_id>`` is the first :data:`_DASHBOARD_DIR_ID_CHARS`
chars of the ``dashboard_id`` (short, to stay under Windows
MAX_PATH), with the bundle tarball as a sibling
(``<device_name>.tar.gz``). :class:`RemoteBuildPath` is the
canonical ``(dashboard_id, device_name)`` key;
:meth:`~RemoteBuildPath.subtree` /
:meth:`~RemoteBuildPath.bundle` build the forward paths and
:func:`parse_from_configuration` is the reverse parse from
:attr:`FirmwareJob.configuration` back to a key (or ``None``
for a non-remote-build job).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# Leading-dot hides the directory from a casual ``ls`` of the
# parent tree, alongside the user's own files.
REMOTE_BUILDS_NAME = ".remote_builds"

# Under ``.esphome/`` so the YAML extract sits in the
# dashboard's hidden artefacts tree rather than at the top
# level of ``<config_dir>``.
REMOTE_BUILDS_SUBDIR = Path(".esphome") / REMOTE_BUILDS_NAME

# Bundle lives outside the extract target so upstream
# ``prepare_bundle_for_compile``'s pre-extract wipe doesn't
# delete it (PR #552).
BUNDLE_SUFFIX = ".tar.gz"


# POSIX-form parts of ``REMOTE_BUILDS_SUBDIR``, pre-split once.
# :attr:`FirmwareJob.configuration` is forward-slash on every
# platform so the reverse parse uses these directly.
_REMOTE_BUILDS_PARTS: tuple[str, ...] = tuple(REMOTE_BUILDS_SUBDIR.as_posix().split("/"))

# Tail segments a valid configuration carries after the prefix:
# dir_id + device_name + YAML filename.
_TAIL_SEGMENT_COUNT = 3

# On-disk directory key: first 8 chars of the dashboard_id. Short keeps the
# subtree path under Windows MAX_PATH; the id stays full-length on the wire.
# Truncation is idempotent (``id[:8][:8] == id[:8]``), so a path recovered by
# :func:`parse_from_configuration` (already 8 chars) renders the same directory.
_DASHBOARD_DIR_ID_CHARS = 8


@dataclass(frozen=True)
class RemoteBuildPath:
    """Canonical ``(dashboard_id, device_name)`` key for a remote-build subtree.

    Frozen + hashable so the cleanup sweep can build a
    :class:`frozenset` of in-flight keys and pass it across
    the executor boundary without worrying about mutation.
    """

    dashboard_id: str
    device_name: str

    @property
    def dir_id(self) -> str:
        """On-disk directory key: first :data:`_DASHBOARD_DIR_ID_CHARS` chars of the id."""
        return self.dashboard_id[:_DASHBOARD_DIR_ID_CHARS]

    def subtree(self, config_dir: Path) -> Path:
        """Return the absolute extract directory under *config_dir*."""
        return config_dir / REMOTE_BUILDS_SUBDIR / self.dir_id / self.device_name

    def bundle(self, config_dir: Path) -> Path:
        """Return the absolute bundle tarball path, sibling to :meth:`subtree`."""
        return (
            config_dir / REMOTE_BUILDS_SUBDIR / self.dir_id / f"{self.device_name}{BUNDLE_SUFFIX}"
        )

    def data_dir(self, dashboard_data_dir: Path) -> Path:
        """Return the ``ESPHOME_DATA_DIR`` the compile subprocess writes into.

        Resolves to
        ``<dashboard_data_dir>/.remote_builds/<dir_id>/.esphome``
        — one shared ``.esphome`` per paired offloader,
        anchored under :attr:`esphome.core.CORE.data_dir` so
        the addon's per-instance ``/data`` volume holds the
        multi-GB ``.platformio`` toolchain + ``build/`` cache
        rather than the user's HA ``/config`` mount.

        Per-dashboard scope (not per-device or single-shared):

        * **Per-device** would re-download the ~1-2 GB
          PlatformIO toolchain for every device. Per-dashboard
          keeps the toolchain warm across submits from the
          same offloader.
        * **Single-shared** across all dashboards reintroduces
          PR #578's basename-collision bug — two offloaders
          each submitting ``kitchen.yaml`` would clobber each
          other's ``storage/kitchen.yaml.json``. The ``dir_id``
          partition is the isolation gate; it's the first 8
          chars of the dashboard_id (~48 bits), so two offloaders
          collide only if their ids share that prefix —
          astronomically unlikely for a handful of offloaders.

        Separate root from :meth:`subtree` — that one is
        anchored on ``config_dir``, this one on
        ``CORE.data_dir`` (the same path on default installs,
        different in HA-addon mode: ``/config/esphome`` vs
        ``/data``). Splitting build artefacts (here) from the
        per-device YAML extract (there) keeps upstream
        :func:`esphome.bundle.prepare_bundle_for_compile`'s
        pre-extract wipe off the deep ``build/<env>/.pioenvs``
        tree (PR #578).
        """
        return dashboard_data_dir / REMOTE_BUILDS_NAME / self.dir_id / ".esphome"


def parse_from_configuration(configuration: str) -> RemoteBuildPath | None:
    """Recover the :class:`RemoteBuildPath` key from *configuration*, or ``None``.

    Reverse of :meth:`RemoteBuildPath.subtree`. ``None`` for
    any path outside the canonical layout — typically a
    locally-submitted job sitting at the top of ``<config_dir>``;
    callers treat ``None`` as "not a remote-build job".

    The recovered ``dashboard_id`` is the on-disk ``dir_id``
    (already truncated to :data:`_DASHBOARD_DIR_ID_CHARS` chars,
    not the full wire id). :attr:`RemoteBuildPath.dir_id` is
    idempotent, so the round-tripped key renders the same paths.
    """
    parts = PurePosixPath(configuration).parts
    expected = _REMOTE_BUILDS_PARTS
    if len(parts) < len(expected) + _TAIL_SEGMENT_COUNT:
        return None
    if parts[: len(expected)] != tuple(expected):
        return None
    return RemoteBuildPath(
        dashboard_id=parts[len(expected)],
        device_name=parts[len(expected) + 1],
    )
