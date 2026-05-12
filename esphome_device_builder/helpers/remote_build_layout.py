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
* :class:`controllers.remote_build.ReceiverController`'s
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
``config_dir`` and return absolute paths under the user's
config tree; :meth:`data_dir` takes the dashboard's
``CORE.data_dir`` (a *different* root on the HA addon,
``/data`` vs ``/config/esphome``) and returns the
``ESPHOME_DATA_DIR`` the compile subprocess writes its
build artefacts into. The reverse factory
(:func:`parse_from_configuration`) takes the relative POSIX
path :attr:`FirmwareJob.configuration` carries and rebuilds
the key. Anything that doesn't match the layout shape (a
locally-submitted job, a hand-edited configuration, a future
call site that bends the path) round-trips through ``None``
so callers can short-circuit cleanly without an
``if len(parts) >= ...`` chain at every call site.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# Single segment used by every layout path. Hidden by the leading
# dot so a casual ``ls`` of the parent tree doesn't show it next
# to the user's own files.
REMOTE_BUILDS_NAME = ".remote_builds"

# Subdirectory under ``<config_dir>/`` where the receiver extracts
# per-device bundles. Living under ``.esphome/`` keeps the YAML
# extraction adjacent to the user's other ``.esphome``-bucketed
# artefacts (StorageJSON, build dirs) and out of the way of a
# casual ``ls`` of ``config_dir``.
REMOTE_BUILDS_SUBDIR = Path(".esphome") / REMOTE_BUILDS_NAME

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

    def data_dir(self, dashboard_data_dir: Path) -> Path:
        """Return the ``ESPHOME_DATA_DIR`` for this per-build compile.

        Resolves to
        ``<dashboard_data_dir>/.remote_builds/<dashboard_id>/.esphome``
        — one ``.esphome`` per paired offloader, anchored
        under whatever the dashboard process is using for its
        own data directory (:attr:`esphome.core.CORE.data_dir`):

        * **Default mode**: ``<config_dir>/.esphome/.remote_builds/<dashboard_id>/.esphome``
          — co-located with the user's local-build artefacts.
        * **HA-addon mode**: ``/data/.remote_builds/<dashboard_id>/.esphome``
          — on the addon's per-instance persistent volume.
          *Critically* not under ``<config_dir>`` (=``/config/esphome``
          on the addon): build artefacts can run to multiple
          gigabytes (``.platformio/`` toolchain + ``build/`` cache)
          and ``/config`` is the user's Home Assistant config
          mount, often a small partition shared with HA core
          and every other addon. ``/data`` is the dedicated
          per-addon volume the addon was given exactly for this
          kind of artefact.
        * **``ESPHOME_DATA_DIR`` env override**:
          ``$ESPHOME_DATA_DIR/.remote_builds/<dashboard_id>/.esphome``.

        Anchoring on ``CORE.data_dir`` rather than on
        ``config_dir`` is the load-bearing detail — the dashboard
        already routes its own build output through the same
        env-override / HA-addon / default fallback chain, so the
        remote-build artefacts land in the volume the operator
        expects to grow.

        Every device from the same dashboard shares one toolchain
        cache (``.platformio/``), one storage keyspace, one
        idedata cache, and one build root; different offloaders
        get independent dirs.

        Why per-dashboard (and not per-device or shared):

        * **Per-device** would re-download the ~1-2 GB
          PlatformIO toolchain on every new device a paired
          offloader submitted. Per-dashboard means device #N
          from that offloader hits a warm cache.
        * **Single shared** across all dashboards would bring
          back the original problem PR #578 was solving:
          two offloaders submitting the same YAML basename
          (e.g. both have ``kitchen.yaml``) collide on
          ``storage/kitchen.yaml.json``, silently delivering
          one offloader's bytes to the other's device. The
          ``dashboard_id`` partition is the load-bearing
          isolation gate.
        * **Survives prepare_bundle_for_compile's cleanup
          walk.** Upstream :func:`esphome.bundle.prepare_bundle_for_compile`
          wipes the non-preserved children of *target_dir*
          (== :meth:`subtree`, the per-device subtree) before
          extracting the new bundle. The per-device subtree
          lives under ``<config_dir>/.esphome/.remote_builds/...``
          and now contains only the YAML — build artefacts are
          one tree away under ``CORE.data_dir``, so the walk
          never sees them. No ``rmtree`` recursion over the
          deep ``build/<env>/.pioenvs/`` tree, no race against
          macOS Finder ``.DS_Store`` re-writes (the original
          ``OSError(ENOTEMPTY, '.../build/.../.pioenvs')``
          report; see #578 follow-up).

        Why pin ``ESPHOME_DATA_DIR`` at all rather than just
        inherit the dashboard's value:

        * The dashboard's value points at *its own* data dir
          (``/data`` on the addon, ``<config_dir>/.esphome``
          in default mode). Without the override, the
          subprocess would write the remote-build sidecar /
          build dir into the same keyspace as the dashboard's
          local builds — same-basename collisions across
          dashboards (and against the user's own local YAMLs)
          silently mix builds.

        Pinning keeps every remote-build artefact under one
        ``dashboard_id``-keyed directory.

        Cleanup asymmetry vs the 6c TTL sweep
        (:func:`helpers.remote_build_cleanup.sweep_remote_builds`):
        the sweep walks ``config_dir / REMOTE_BUILDS_SUBDIR``
        and reclaims cold per-device subtrees (the YAML
        extract + bundle sibling). The shared per-dashboard
        ``.esphome/`` lives under ``CORE.data_dir`` — a
        *different* root on the HA addon (``/data`` vs
        ``/config/esphome``) — and is not currently walked by
        the sweep. That asymmetry is intentional for the
        toolchain (``.platformio/``, multi-GB, the whole point
        of per-dashboard scope is keeping it warm across
        submits); per-device build dirs under ``build/<name>/``
        and their ``storage/`` / ``idedata/`` sidecars are
        currently orphaned when their per-device subtree gets
        reclaimed. Tracking that as a follow-up — a future
        cleanup pass keyed on "no devices remain under this
        ``dashboard_id``" can walk
        ``<dashboard_data_dir>/.remote_builds/<dashboard_id>/.esphome/``
        and reclaim the entire offloader's build cache on
        unpair / TTL expiry.

        Read side: :func:`helpers.build_artifacts.load_build_artifacts`
        derives ``data_dir`` back from the configuration string
        via :func:`parse_from_configuration`, then calls this
        method — same value as the write side because both
        route ``Path(CORE.data_dir)`` through here.
        """
        return dashboard_data_dir / REMOTE_BUILDS_NAME / self.dashboard_id / ".esphome"


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
