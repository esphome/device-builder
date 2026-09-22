"""E2E: a config-only rename leaves the firmware on its old hostname; the install still lands."""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from zeroconf import DNSAddress, Zeroconf, current_time_millis
from zeroconf.const import _CLASS_IN, _TYPE_A

from esphome_device_builder.device_builder import DeviceBuilder
from esphome_device_builder.models import DeviceState, EventType, JobStatus, JobType

from ..conftest import MakeSettingsFactory, record_argv_esphome

# ``api:`` so the row loads an OTA-capable integration and gets cache args at all.
_YAML = """\
esphome:
  name: kitchen

esp8266:
  board: esp01_1m

wifi:
  ssid: lan
  password: password1

api:
"""


@pytest.fixture
async def dashboard(
    make_settings: MakeSettingsFactory,
    _hermetic_lifecycle: None,
    tmp_path: Path,
) -> Any:
    """Real ``DeviceBuilder`` with one device tracked from the initial scan."""
    (tmp_path / "kitchen.yaml").write_text(_YAML, encoding="utf-8")
    settings = make_settings(with_core_path=True)
    settings.using_password = False
    db = DeviceBuilder(settings)
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


def _completed_job(db: DeviceBuilder, job_type: JobType) -> asyncio.Future:
    """Future resolved by the JOB_COMPLETED bus event for the next *job_type* job."""
    done: asyncio.Future = asyncio.get_running_loop().create_future()

    def _on_completed(event: Any) -> None:
        job = event.data["job"]
        if job.job_type is job_type and job.status is JobStatus.COMPLETED and not done.done():
            done.set_result(job)

    unsub = db.bus.add_listener(EventType.JOB_COMPLETED, _on_completed)
    done.add_done_callback(lambda _f: unsub())
    return done


def _cache_a_record(zc: Zeroconf, host: str, ip: str) -> None:
    """Seed *zc*'s cache as a live announce of *host* at *ip* would."""
    record = DNSAddress(
        name=host,
        type_=_TYPE_A,
        class_=_CLASS_IN,
        ttl=120,
        address=socket.inet_aton(ip),
        created=current_time_millis(),
    )
    zc.cache.async_add_records([record])


async def test_config_only_rename_then_install_targets_the_old_hostname(
    dashboard: DeviceBuilder, tmp_path: Path
) -> None:
    """The reporter's loop: rename without a flash, then install (#2730)."""
    db = dashboard
    assert db.firmware is not None
    assert db.devices is not None
    monitor = db.devices._state_monitor
    argv_log = tmp_path / "argv.jsonl"
    record_argv_esphome(db.firmware.state, argv_log)

    # The firmware answered ``kitchen`` at .50 before the rename.
    monitor.apply("kitchen", DeviceState.ONLINE, "mdns", claim=True)
    monitor.apply_ip("kitchen", "192.168.1.50")

    result = await db.devices.rename_device(
        configuration="kitchen.yaml", new_name="livingroom", config_only=True
    )

    assert result == {"configuration": "livingroom.yaml", "job": None}
    device = db.devices.get_by_configuration("livingroom.yaml")
    assert device is not None
    # The live row is what the address-cache build and the reviver read.
    assert device.deployed_name == "kitchen"

    # A DHCP move since: what answers ``kitchen.local`` now is .60, not the
    # persisted .50, and it's the old name the firmware still broadcasts.
    zc = Zeroconf(interfaces=["127.0.0.1"])
    try:
        _cache_a_record(zc, "kitchen.local.", "192.168.1.60")
        monitor.mdns._zeroconf = MagicMock(zeroconf=zc)

        upload_done = _completed_job(db, JobType.UPLOAD)
        await db.firmware.install(configuration="livingroom.yaml", port="OTA")
        await asyncio.wait_for(upload_done, timeout=5.0)
    finally:
        zc.close()

    invocations = [json.loads(line) for line in argv_log.read_text().splitlines()]
    upload = next(argv for argv in invocations if "upload" in argv)
    cache_arg = upload[upload.index("--mdns-address-cache") + 1]
    assert cache_arg == "livingroom.local=192.168.1.60"
    # The landed flash retires the record, on the indexed row and on disk. The
    # compile's post-job reload rebuilt the row, so read it back the way the
    # readers do rather than through the pre-install handle.
    device = db.devices.get_by_configuration("livingroom.yaml")
    assert device is not None
    assert device.deployed_name == ""
    assert "deployed_name" not in db.devices._metadata_store.get("livingroom.yaml")
