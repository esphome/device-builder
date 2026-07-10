"""E2E: offline install defers the flash, a fake mDNS wake delivers it."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from esphome_device_builder.device_builder import DeviceBuilder
from esphome_device_builder.models import DeviceState, EventType, JobStatus, JobType

from ..conftest import MakeSettingsFactory, record_argv_esphome

_ESP8266_YAML = "esphome:\n  name: kitchen\n\nesp8266:\n  board: esp01_1m\n"


@pytest.fixture
async def dashboard(
    make_settings: MakeSettingsFactory,
    _hermetic_lifecycle: None,
    tmp_path: Path,
) -> Any:
    """Real ``DeviceBuilder`` with one esp8266 device tracked from the initial scan."""
    (tmp_path / "kitchen.yaml").write_text(_ESP8266_YAML, encoding="utf-8")
    settings = make_settings(with_core_path=True)
    settings.using_password = False
    db = DeviceBuilder(settings)
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


def _announce(db: DeviceBuilder, state: DeviceState, source: str) -> None:
    """Drive the production monitor path a zeroconf announcement / ping sweep takes."""
    assert db.devices is not None
    db.devices._state_monitor.apply("kitchen", state, source, claim=True)


def _completed_job(db: DeviceBuilder, job_type: JobType) -> asyncio.Future:
    """Future resolved by the JOB_COMPLETED bus event for the next *job_type* job.

    Registered before the trigger so the completion can't be missed.
    """
    done: asyncio.Future = asyncio.get_running_loop().create_future()

    def _on_completed(event: Any) -> None:
        job = event.data["job"]
        if job.job_type is job_type and job.status is JobStatus.COMPLETED and not done.done():
            done.set_result(job)

    unsub = db.bus.add_listener(EventType.JOB_COMPLETED, _on_completed)
    done.add_done_callback(lambda _f: unsub())
    return done


async def _wait_queued_update(db: DeviceBuilder, *, expected: bool, timeout: float = 5.0) -> None:
    """Wait for ``queued_update`` == *expected*, driven by DEVICE_UPDATED events."""
    assert db.devices is not None
    flag: asyncio.Future = asyncio.get_running_loop().create_future()

    def _check(_event: Any = None) -> None:
        device = db.devices.get_by_configuration("kitchen.yaml")
        if (
            device is not None
            and device.runtime_state.queued_update is expected
            and not flag.done()
        ):
            flag.set_result(None)

    unsub = db.bus.add_listener(EventType.DEVICE_UPDATED, _check)
    try:
        # The flip may have landed before the listener attached.
        _check()
        await asyncio.wait_for(flag, timeout)
    finally:
        unsub()


async def test_offline_install_defers_then_flashes_on_wake(
    dashboard: DeviceBuilder, tmp_path: Path
) -> None:
    """Install-while-offline compiles only; the wake announcement delivers the flash."""
    db = dashboard
    assert db.firmware is not None
    assert db.devices is not None
    argv_log = tmp_path / "argv.jsonl"
    record_argv_esphome(db.firmware.state, argv_log)
    _announce(db, DeviceState.OFFLINE, "ping")

    compile_done = _completed_job(db, JobType.COMPILE)
    job = await db.firmware.install(configuration="kitchen.yaml", port="OTA")

    assert job.job_type is JobType.COMPILE
    assert job.is_deferred_install is True
    await asyncio.wait_for(compile_done, timeout=5.0)
    await _wait_queued_update(db, expected=True)

    invocations = [json.loads(line) for line in argv_log.read_text().splitlines()]
    assert [argv[1] for argv in invocations] == ["compile"]
    assert not any(j.job_type is JobType.UPLOAD for j in db.firmware.state.jobs.values())

    # The fake announcement flips the device ONLINE through the same
    # monitor -> state-callback -> DEVICE_STATE_CHANGED path zeroconf drives.
    upload_done = _completed_job(db, JobType.UPLOAD)
    _announce(db, DeviceState.ONLINE, "mdns")
    await asyncio.wait_for(upload_done, timeout=5.0)
    await _wait_queued_update(db, expected=False)

    invocations = [json.loads(line) for line in argv_log.read_text().splitlines()]
    assert invocations[-1][0] == "--dashboard"
    assert invocations[-1][1:] == ["upload", str(tmp_path / "kitchen.yaml"), "--device", "OTA"]
