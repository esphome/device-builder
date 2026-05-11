"""Tests for ``FirmwareController._compose_subprocess_env``.

The env composition forks on the job's ``configuration`` shape:
local jobs inherit the dashboard's deployment-mode context
unchanged, receiver-side remote-build jobs pin
``ESPHOME_DATA_DIR`` to the per-build subtree so esphome writes
storage / idedata / build under one ``(dashboard_id, device)``-keyed
directory. The fork is small but load-bearing — without the
override the download-time reader looks at a path the subprocess
didn't write to and the offloader sees silent ``build_dir_missing``
rejects on every install.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from esphome_device_builder.controllers.firmware.constants import (
    ESPHOME_SUBPROCESS_ENV,
)
from esphome_device_builder.models import FirmwareJob, JobType

if TYPE_CHECKING:
    from .conftest import FirmwareControllerFactory


def _make_job(*, configuration: str) -> FirmwareJob:
    """Build a minimal :class:`FirmwareJob` keyed on *configuration*."""
    return FirmwareJob(
        job_id="j1",
        configuration=configuration,
        job_type=JobType.COMPILE,
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


def test_remote_build_job_pins_data_dir_to_per_build_subtree(
    firmware_controller_factory: FirmwareControllerFactory,
    tmp_path: Path,
) -> None:
    """A receiver-side remote-build configuration pins ``ESPHOME_DATA_DIR``.

    The configuration is the relative POSIX path the receiver-side
    submit_job dispatch sets on the :class:`FirmwareJob`
    (``.esphome/.remote_builds/<dashboard_id>/<device>/<device>.yaml``).
    The env override points at the per-build subtree under the
    controller's settings ``config_dir`` so esphome's
    ``CORE.data_dir`` resolves there and storage / idedata / build
    all land under one ``(dashboard_id, device)``-keyed directory.
    """
    controller = firmware_controller_factory(with_settings=True)
    configuration = ".esphome/.remote_builds/dashboard-alpha/kitchen/kitchen.yaml"
    env = controller._compose_subprocess_env(_make_job(configuration=configuration))

    expected = (
        controller._db.settings.config_dir
        / ".esphome"
        / ".remote_builds"
        / "dashboard-alpha"
        / "kitchen"
    )
    assert env["ESPHOME_DATA_DIR"] == str(expected)
    # The override is the only data-dir-related change; the
    # ANSI / unbuffered overlays still land.
    for key, value in ESPHOME_SUBPROCESS_ENV.items():
        assert env[key] == value


def test_malformed_remote_build_path_falls_through_to_local(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A configuration that doesn't parse as a remote-build path stays local.

    The layout parser returns ``None`` for any path that doesn't
    match ``.esphome/.remote_builds/<dashboard_id>/<device>/<file>``
    — a 4-segment shorthand like
    ``.esphome/.remote_builds/<id>/kitchen.yaml`` (no device
    subtree) doesn't qualify and the env override skips. Pins
    the contract that ``ESPHOME_DATA_DIR`` is only pinned when
    we know we're looking at the canonical layout the writer
    produces.
    """
    controller = firmware_controller_factory(with_settings=True)
    configuration = ".esphome/.remote_builds/dashboard-alpha/kitchen.yaml"
    env = controller._compose_subprocess_env(_make_job(configuration=configuration))

    assert env.get("ESPHOME_DATA_DIR") == os.environ.get("ESPHOME_DATA_DIR")
