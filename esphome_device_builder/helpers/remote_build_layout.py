"""
Single source of truth for the receiver-side remote-build on-disk layout.

Per-device YAML extract lives at

    ``<config_dir>/.esphome/.remote_builds/<dashboard_id>/<device_name>/``

with the bundle tarball as a sibling
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
# dashboard_id + device_name + YAML filename.
_TAIL_SEGMENT_COUNT = 3


@dataclass(frozen=True)
class RemoteBuildPath:
    """Canonical ``(dashboard_id, device_name)`` key for a remote-build subtree.

    Frozen + hashable so the cleanup sweep can build a
    :class:`frozenset` of in-flight keys and pass it across
    the executor boundary without worrying about mutation.
    """

    dashboard_id: str
    device_name: str

    def subtree(self, config_dir: Path) -> Path:
        """Return the absolute extract directory under *config_dir*."""
        return config_dir / REMOTE_BUILDS_SUBDIR / self.dashboard_id / self.device_name

    def bundle(self, config_dir: Path) -> Path:
        """Return the absolute bundle tarball path, sibling to :meth:`subtree`."""
        return (
            config_dir
            / REMOTE_BUILDS_SUBDIR
            / self.dashboard_id
            / f"{self.device_name}{BUNDLE_SUFFIX}"
        )

    def data_dir(self, dashboard_data_dir: Path) -> Path:
        """Return the ``ESPHOME_DATA_DIR`` the compile subprocess writes into.

        Resolves to
        ``<dashboard_data_dir>/.remote_builds/<dashboard_id>/.esphome``
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
          other's ``storage/kitchen.yaml.json``. The
          ``dashboard_id`` partition is the isolation gate.

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
        return dashboard_data_dir / REMOTE_BUILDS_NAME / self.dashboard_id / ".esphome"


def parse_from_configuration(configuration: str) -> RemoteBuildPath | None:
    """Recover the :class:`RemoteBuildPath` key from *configuration*, or ``None``.

    Reverse of :meth:`RemoteBuildPath.subtree`. ``None`` for
    any path outside the canonical layout — typically a
    locally-submitted job sitting at the top of ``<config_dir>``;
    callers treat ``None`` as "not a remote-build job".
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
