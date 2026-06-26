"""Tests for ``FirmwareController._compose_subprocess_env``.

The env composition forks on the job's ``configuration`` shape:
local jobs inherit the dashboard's deployment-mode context
unchanged, receiver-side remote-build jobs pin
``ESPHOME_DATA_DIR`` to a per-dashboard subdirectory of
``CORE.data_dir`` so esphome writes storage / idedata / build
under one ``dashboard_id``-keyed directory on the same volume
the dashboard already uses for its own builds (``/data`` on
the HA addon, ``<config_dir>/.esphome`` in default mode). The
fork is small but load-bearing — without the override the
download-time reader looks at a path the subprocess didn't
write to and the offloader sees silent ``build_dir_missing``
rejects on every install.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from esphome.core import CORE

from esphome_device_builder.controllers.firmware.constants import (
    ESPHOME_SUBPROCESS_ENV,
)
from esphome_device_builder.models import FirmwareJob, JobType

if TYPE_CHECKING:
    from .conftest import FirmwareControllerFactory


def _make_job(
    *,
    configuration: str,
    job_type: JobType = JobType.COMPILE,
) -> FirmwareJob:
    """Build a minimal :class:`FirmwareJob` keyed on *configuration*."""
    return FirmwareJob(
        job_id="j1",
        configuration=configuration,
        job_type=job_type,
    )


def test_local_job_env_does_not_override_data_dir(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A bare-basename configuration leaves the dashboard's data_dir alone.

    The local-build subprocess inherits whatever ``ESPHOME_DATA_DIR``
    the dashboard process is running under (unset in default
    mode, ``/data`` in HA-addon mode). We only set the env var
    for receiver-side remote-build jobs.
    """
    controller = firmware_controller_factory(with_settings=True)
    env = controller._compose_subprocess_env(_make_job(configuration="kitchen.yaml"))

    assert env.get("ESPHOME_DATA_DIR") == os.environ.get("ESPHOME_DATA_DIR")
    # ``ESPHOME_SUBPROCESS_ENV`` overlays land regardless.
    for key, value in ESPHOME_SUBPROCESS_ENV.items():
        assert env[key] == value


def test_remote_build_job_pins_data_dir_to_per_dashboard_esphome(
    firmware_controller_factory: FirmwareControllerFactory,
    tmp_path: Path,
) -> None:
    """A receiver-side remote-build configuration pins ``ESPHOME_DATA_DIR``.

    The configuration is the relative POSIX path the receiver-side
    submit_job dispatch sets on the :class:`FirmwareJob`
    (``.esphome/.remote_builds/<dir_id>/<device>/<device>.yaml``).
    The env override points at the per-dashboard
    ``<CORE.data_dir>/.remote_builds/<dir_id>/.esphome``
    directory: one toolchain cache + storage keyspace shared
    across every device that offloader submits, isolated
    from the dashboard's local-build keyspace AND from other
    offloaders. The ``dashboard_id`` partition prevents the
    same-basename collision (two offloaders' ``kitchen.yaml``
    would otherwise mix sidecars).

    Anchoring on ``CORE.data_dir`` rather than on
    ``settings.config_dir`` matters in HA-addon mode: on the
    addon ``CORE.data_dir`` is ``/data`` (the addon's
    per-instance persistent volume), so the toolchain + build
    cache lands there rather than on ``/config`` (the user's
    Home Assistant config mount, often a small partition).
    Tests run in default mode where ``CORE.data_dir`` is
    ``<config_dir>/.esphome`` so the resolved value is
    equivalent — but the assertion is built from
    ``CORE.data_dir`` to pin the *anchor*, not just the path.
    """
    controller = firmware_controller_factory(with_settings=True)
    configuration = ".esphome/.remote_builds/a1b2c3d4/kitchen/kitchen.yaml"
    env = controller._compose_subprocess_env(_make_job(configuration=configuration))

    expected = Path(CORE.data_dir) / ".remote_builds" / "a1b2c3d4" / ".esphome"
    assert env["ESPHOME_DATA_DIR"] == str(expected)
    # The override is the only data-dir-related change; the
    # ANSI / unbuffered overlays still land.
    for key, value in ESPHOME_SUBPROCESS_ENV.items():
        assert env[key] == value


def test_remote_build_clean_job_pins_data_dir_to_per_dashboard_esphome(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A receiver-side CLEAN job with a remote-build configuration pins the data dir.

    Pins the seam the ``firmware/clean`` fan-out depends on:
    the env override fires off the configuration path, not the
    job type, so a CLEAN job pointing at the per-offloader
    subtree's YAML resolves to the same per-dashboard
    ``ESPHOME_DATA_DIR`` a COMPILE job would. ``esphome clean``
    then sees ``CORE.data_dir`` rooted there and wipes
    ``<that>/build/<device_name>/`` — exactly the directory
    the operator's "Clean build files" click expects to drop
    on each connected receiver.

    Tested explicitly with ``JobType.CLEAN`` so a future
    refactor that adds a ``job_type``-keyed branch in
    ``_compose_subprocess_env`` and accidentally skips the
    override for CLEAN doesn't slip past review — the
    silent regression mode would be "clean ran but pointed
    at the wrong data dir, so nothing got removed."
    """
    controller = firmware_controller_factory(with_settings=True)
    configuration = ".esphome/.remote_builds/a1b2c3d4/kitchen/kitchen.yaml"
    env = controller._compose_subprocess_env(
        _make_job(configuration=configuration, job_type=JobType.CLEAN)
    )

    expected = Path(CORE.data_dir) / ".remote_builds" / "a1b2c3d4" / ".esphome"
    assert env["ESPHOME_DATA_DIR"] == str(expected)


def test_remote_build_job_drops_ha_addon_marker(
    firmware_controller_factory: FirmwareControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A remote-build job drops ``ESPHOME_IS_HA_ADDON`` so the child honours the override."""
    monkeypatch.setenv("ESPHOME_IS_HA_ADDON", "1")
    controller = firmware_controller_factory(with_settings=True)
    configuration = ".esphome/.remote_builds/a1b2c3d4/kitchen/kitchen.yaml"
    env = controller._compose_subprocess_env(_make_job(configuration=configuration))

    assert "ESPHOME_IS_HA_ADDON" not in env


def test_local_job_keeps_ha_addon_marker(
    firmware_controller_factory: FirmwareControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local job leaves ``ESPHOME_IS_HA_ADDON`` untouched so local builds still target /data."""
    monkeypatch.setenv("ESPHOME_IS_HA_ADDON", "1")
    controller = firmware_controller_factory(with_settings=True)
    env = controller._compose_subprocess_env(_make_job(configuration="kitchen.yaml"))

    assert env["ESPHOME_IS_HA_ADDON"] == "1"


def test_malformed_remote_build_path_falls_through_to_local(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A configuration that doesn't parse as a remote-build path stays local.

    The layout parser returns ``None`` for any path that doesn't
    match ``.esphome/.remote_builds/<dir_id>/<device>/<file>``
    — a 4-segment shorthand like
    ``.esphome/.remote_builds/<dir_id>/kitchen.yaml`` (no device
    subtree) doesn't qualify and the env override skips. Pins
    the contract that ``ESPHOME_DATA_DIR`` is only pinned when
    we know we're looking at the canonical layout the writer
    produces.
    """
    controller = firmware_controller_factory(with_settings=True)
    configuration = ".esphome/.remote_builds/a1b2c3d4/kitchen.yaml"
    env = controller._compose_subprocess_env(_make_job(configuration=configuration))

    assert env.get("ESPHOME_DATA_DIR") == os.environ.get("ESPHOME_DATA_DIR")
