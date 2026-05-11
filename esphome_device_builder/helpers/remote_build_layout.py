"""
Single source of truth for the receiver-side remote-build on-disk layout.

Every remote-build subtree the receiver writes lives at

    ``<config_dir>/.esphome/.remote_builds/<dashboard_id>/<device_name>/``

with its bundle tarball as a sibling at

    ``<config_dir>/.esphome/.remote_builds/<dashboard_id>/<device_name>.tar.gz``

Three sites need to know this shape:

* :mod:`controllers.remote_build.submit_job` writes it
  (constructing ``target_dir`` and ``bundle_path`` per submission).
* :mod:`helpers.remote_build_cleanup` walks it
  (iterating ``<dashboard_id>/<device_name>/`` subtrees for the
  6c TTL sweep).
* :class:`controllers.remote_build.RemoteBuildController`'s
  cleanup loop parses :attr:`FirmwareJob.configuration` back
  into ``(dashboard_id, device_name)`` to skip in-flight
  subtrees from the sweep.

Without a shared module each site encodes the shape its own
way — three implicit ``Path`` constructions plus a fragile
``PurePosixPath(...).parts[...]`` reverse-parse. Drift between
them silently breaks the sweep (deletes a subtree that's still
in-flight) or the writer (writes to a path the sweep doesn't
recognise). Consolidating here means the shape lives in
exactly one file: change it here once, every consumer follows.

:class:`RemoteBuildPath` is the canonical key. The forward
methods (:meth:`subtree`, :meth:`bundle`) take a
``config_dir`` and return absolute paths; the reverse
factory (:func:`parse_from_configuration`) takes the
relative POSIX path :attr:`FirmwareJob.configuration` carries
and rebuilds the key. Anything that doesn't match the layout
shape (a locally-submitted job, a hand-edited configuration,
a future call site that bends the path) round-trips through
``None`` so callers can short-circuit cleanly without an
``if len(parts) >= ...`` chain at every call site.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# Subdirectory under ``<config_dir>/.esphome/`` where remote-peer
# build artefacts land. Hidden by the leading dot so a casual
# ``ls`` of the user's main config tree doesn't show it next to
# their own YAMLs; living under ``.esphome/`` keeps it adjacent
# to other build artefacts (StorageJSON, build dirs).
REMOTE_BUILDS_SUBDIR = Path(".esphome") / ".remote_builds"

# Suffix the bundle tarball lives at, as a sibling of the
# extracted build subtree (PR #552 moved the bundle outside the
# extract target so upstream
# :func:`esphome.bundle.prepare_bundle_for_compile`'s wipe step
# doesn't delete it).
BUNDLE_SUFFIX = ".tar.gz"


# POSIX-form path segments of ``REMOTE_BUILDS_SUBDIR``. Computed
# once at module load so the reverse-parse path doesn't pay a
# ``Path(...).parts`` rebuild on every :attr:`FirmwareJob.configuration`
# lookup. Locked to the POSIX shape because
# :attr:`FirmwareJob.configuration` is serialised as a forward-
# slash path on every platform (a Windows receiver still
# stores ``.esphome/.remote_builds/...``).
_REMOTE_BUILDS_PARTS: tuple[str, ...] = tuple(REMOTE_BUILDS_SUBDIR.as_posix().split("/"))

# Number of path segments AFTER ``_REMOTE_BUILDS_PARTS`` that a
# valid configuration carries: ``dashboard_id`` (1) +
# ``device_name`` (2) + at least one entry inside the device
# subtree (3 — the YAML filename the writer extracts). Anything
# shorter is malformed; the parse returns ``None`` so callers
# can skip it.
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
        """Return the absolute build-subtree directory under *config_dir*.

        Used by the writer (:class:`SubmitJobReceiver`) to lay
        down the extract target and by the sweeper to point
        :func:`shutil.rmtree` at the right path when reclaiming
        a cold entry.
        """
        return config_dir / REMOTE_BUILDS_SUBDIR / self.dashboard_id / self.device_name

    def bundle(self, config_dir: Path) -> Path:
        """Return the absolute bundle tarball sibling under *config_dir*.

        The bundle lives one level up from
        :meth:`subtree` (sibling of the subtree directory, not
        inside it) so upstream
        :func:`esphome.bundle.prepare_bundle_for_compile`'s wipe
        step can't delete it mid-extract; see PR #552.
        """
        return (
            config_dir
            / REMOTE_BUILDS_SUBDIR
            / self.dashboard_id
            / f"{self.device_name}{BUNDLE_SUFFIX}"
        )


def parse_from_configuration(configuration: str) -> RemoteBuildPath | None:
    """Recover the :class:`RemoteBuildPath` key from *configuration*, or ``None``.

    Reverse of :meth:`RemoteBuildPath.subtree` for the case
    where the caller has a :attr:`FirmwareJob.configuration`
    relative-POSIX path and wants to know which subtree it
    belongs to. Returns ``None`` for any path that doesn't
    match the canonical layout — typically a locally-submitted
    job whose configuration sits at the top level of
    ``<config_dir>`` rather than under the remote-builds root.
    Callers that read this return value should treat ``None``
    as "not a remote-build job" and skip whatever scan they're
    running.

    The layout's path segments are checked positionally
    against :data:`_REMOTE_BUILDS_PARTS` so a future rename of
    the subdirectory ripples here automatically without a
    cross-file find-and-replace.
    """
    parts = PurePosixPath(configuration).parts
    expected = _REMOTE_BUILDS_PARTS
    # Layout: <expected...>/<dashboard_id>/<device_name>/<yaml>.
    # That's three tail segments after the root prefix:
    # dashboard_id (1), device_name (2), and at least one
    # entry inside the device subtree (3 — the YAML filename
    # the writer extracts). A 4-segment path like
    # ``.esphome/.remote_builds/alpha/kitchen.yaml`` is the
    # writer never producing the device subtree it should
    # have; treat as malformed and return ``None``.
    if len(parts) < len(expected) + _TAIL_SEGMENT_COUNT:
        return None
    if parts[: len(expected)] != tuple(expected):
        return None
    return RemoteBuildPath(
        dashboard_id=parts[len(expected)],
        device_name=parts[len(expected) + 1],
    )
