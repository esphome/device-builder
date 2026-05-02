"""
Tests for ``FirmwareController._check_rename_lock``.

A rename touches two YAML files at different points in its lifetime:
- the *old* configuration it's reading from (``configuration``), and
- the *new* configuration it'll write on install success
  (``new_name + ".yaml"``).

Any other firmware job that touches either name would either fight
for the same file or end up flashing a half-renamed device. These
tests pin down the lock policy:

- Compile / install / upload / clean on either side → rejected.
- Another rename whose old or new name overlaps → rejected.
- Fresh rename on the same old config → allowed (supersede path).
- Independent jobs on unrelated configs → allowed.
- Once the rename leaves QUEUED/RUNNING, the lock lifts.
"""

from __future__ import annotations

import pytest

from esphome_device_builder.controllers.firmware import FirmwareController
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import (
    ErrorCode,
    FirmwareJob,
    JobStatus,
    JobType,
)


def _controller(*active: FirmwareJob) -> FirmwareController:
    """Build a stub controller with just the bits ``_check_rename_lock`` reads."""
    controller = FirmwareController.__new__(FirmwareController)
    controller._jobs = {j.job_id: j for j in active}
    return controller


def _job(
    job_id: str,
    configuration: str,
    job_type: JobType,
    *,
    new_name: str = "",
    status: JobStatus = JobStatus.QUEUED,
) -> FirmwareJob:
    return FirmwareJob(
        job_id=job_id,
        configuration=configuration,
        job_type=job_type,
        status=status,
        new_name=new_name,
    )


def test_install_on_old_name_is_rejected() -> None:
    rename = _job("rn1", "kitchen.yaml", JobType.RENAME, new_name="livingroom")
    controller = _controller(rename)
    new = _job("inst1", "kitchen.yaml", JobType.INSTALL)

    with pytest.raises(CommandError) as excinfo:
        controller._check_rename_lock(new)

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "kitchen.yaml" in excinfo.value.message
    assert "livingroom.yaml" in excinfo.value.message


def test_install_on_new_name_is_rejected() -> None:
    rename = _job("rn1", "kitchen.yaml", JobType.RENAME, new_name="livingroom")
    controller = _controller(rename)
    new = _job("inst1", "livingroom.yaml", JobType.INSTALL)

    with pytest.raises(CommandError):
        controller._check_rename_lock(new)


def test_compile_on_unrelated_config_is_allowed() -> None:
    rename = _job("rn1", "kitchen.yaml", JobType.RENAME, new_name="livingroom")
    controller = _controller(rename)

    controller._check_rename_lock(_job("c1", "garage.yaml", JobType.COMPILE))


def test_rename_targeting_same_new_name_is_rejected() -> None:
    """Two renames pointing at the same target name would fight to write it."""
    rename = _job("rn1", "kitchen.yaml", JobType.RENAME, new_name="livingroom")
    controller = _controller(rename)
    second = _job("rn2", "garage.yaml", JobType.RENAME, new_name="livingroom")

    with pytest.raises(CommandError):
        controller._check_rename_lock(second)


def test_rename_retry_on_same_old_config_is_allowed() -> None:
    """Fresh rename on the same OLD config goes through so supersede can cancel-and-replace."""
    rename = _job("rn1", "kitchen.yaml", JobType.RENAME, new_name="livingroom")
    controller = _controller(rename)
    retry = _job("rn2", "kitchen.yaml", JobType.RENAME, new_name="bedroom")

    controller._check_rename_lock(retry)


def test_lock_lifts_when_rename_terminates() -> None:
    """Terminal-status rename no longer holds the lock."""
    rename = _job(
        "rn1",
        "kitchen.yaml",
        JobType.RENAME,
        new_name="livingroom",
        status=JobStatus.COMPLETED,
    )
    controller = _controller(rename)

    controller._check_rename_lock(_job("inst1", "kitchen.yaml", JobType.INSTALL))
    controller._check_rename_lock(_job("inst2", "livingroom.yaml", JobType.INSTALL))


def test_running_rename_blocks_install() -> None:
    """RUNNING jobs hold the lock just like QUEUED ones do."""
    rename = _job(
        "rn1",
        "kitchen.yaml",
        JobType.RENAME,
        new_name="livingroom",
        status=JobStatus.RUNNING,
    )
    controller = _controller(rename)

    with pytest.raises(CommandError):
        controller._check_rename_lock(_job("inst1", "kitchen.yaml", JobType.INSTALL))


@pytest.mark.asyncio
async def test_install_bulk_skips_locked_configs_and_queues_the_rest() -> None:
    """A rename-locked device in a bulk request must not abort the others.

    Bulk install is the user pattern for "update everything that has
    pending changes" — if a single device is mid-rename, queueing
    should still go ahead for every other selected device. This is
    the regression guard for that.
    """
    from unittest.mock import AsyncMock

    rename = _job(
        "rn1",
        "kitchen.yaml",
        JobType.RENAME,
        new_name="livingroom",
        status=JobStatus.RUNNING,
    )
    controller = _controller(rename)
    controller._queue = AsyncMock()
    controller._db = type(
        "DB",
        (),
        {
            "bus": type("Bus", (), {"fire": lambda *a, **kw: None})(),
            # ``install_bulk`` calls ``_validate_configurations_boundary``
            # to reject traversal payloads at the WS boundary; pass-
            # through stub here, the test isn't exercising traversal.
            "settings": type(
                "Settings",
                (),
                {"rel_path": lambda self, *parts: None},
            )(),
        },
    )()
    controller._persist_jobs = AsyncMock()
    controller._supersede_active_jobs = AsyncMock()

    queued = await controller.install_bulk(
        configurations=["kitchen.yaml", "garage.yaml", "livingroom.yaml", "office.yaml"]
    )

    # ``kitchen.yaml`` (rename source) and ``livingroom.yaml`` (rename
    # target) both clash with the in-flight rename and skip; the
    # other two queue normally.
    queued_configs = sorted(j.configuration for j in queued)
    assert queued_configs == ["garage.yaml", "office.yaml"]
