"""Tests for the offloader-side bundle phase's job-log streaming."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from esphome_device_builder.controllers.firmware import bundle_phase
from esphome_device_builder.models import EventType

from .conftest import _capture_local_events, _make_remote_job

if TYPE_CHECKING:
    from .conftest import FirmwareControllerFactory


async def test_run_bundle_phase_streams_output_and_notices(
    firmware_controller_factory: FirmwareControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bundle output lands in ``job.output`` live, framed by phase notices."""
    controller = firmware_controller_factory()
    captured = _capture_local_events(controller)

    async def _fake_bundle(yaml_path: Any, *, on_output: Any = None) -> bytes:
        assert on_output is not None
        on_output("INFO Reading configuration...\n")
        return b"FAKEBUNDLE"

    monkeypatch.setattr(bundle_phase, "build_yaml_bundle", _fake_bundle)
    job = _make_remote_job()

    result = await bundle_phase.run_bundle_phase(controller, job, asyncio.Event())

    assert result == b"FAKEBUNDLE"
    assert job.output[0] == "*** building configuration bundle for remote build ***\n"
    assert "INFO Reading configuration...\n" in job.output
    assert "bundle ready (0 KiB); sending to build server" in job.output[-1]
    assert any(
        e["line"] == "INFO Reading configuration...\n" for e in captured[EventType.JOB_OUTPUT]
    )


async def test_run_bundle_phase_cancel_event_cancels_bundle(
    firmware_controller_factory: FirmwareControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A set cancel event wins the race and the bundle task is cancelled."""
    controller = firmware_controller_factory()
    _capture_local_events(controller)
    bundle_cancelled = asyncio.Event()

    async def _hang_bundle(yaml_path: Any, *, on_output: Any = None) -> bytes:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            bundle_cancelled.set()
            raise
        raise AssertionError("unreachable")

    monkeypatch.setattr(bundle_phase, "build_yaml_bundle", _hang_bundle)
    job = _make_remote_job()
    cancel_event = asyncio.Event()
    cancel_event.set()

    result = await bundle_phase.run_bundle_phase(controller, job, cancel_event)

    assert result is None
    assert bundle_cancelled.is_set()


async def test_run_bundle_phase_cancel_wins_even_when_bundle_completes(
    firmware_controller_factory: FirmwareControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancel landing in the same tick as bundle completion still cancels."""
    controller = firmware_controller_factory()
    _capture_local_events(controller)

    async def _instant_bundle(yaml_path: Any, *, on_output: Any = None) -> bytes:
        return b"FAKEBUNDLE"

    monkeypatch.setattr(bundle_phase, "build_yaml_bundle", _instant_bundle)
    job = _make_remote_job()
    cancel_event = asyncio.Event()
    cancel_event.set()

    assert await bundle_phase.run_bundle_phase(controller, job, cancel_event) is None
    assert not any("bundle ready" in line for line in job.output)


async def test_run_bundle_phase_heartbeat_ticks_while_bundle_runs(
    firmware_controller_factory: FirmwareControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A silent bundle still produces still-building notices in the job log."""
    controller = firmware_controller_factory()
    _capture_local_events(controller)
    monkeypatch.setattr(bundle_phase, "_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    release = asyncio.Event()

    async def _slow_bundle(yaml_path: Any, *, on_output: Any = None) -> bytes:
        await release.wait()
        return b"FAKEBUNDLE"

    monkeypatch.setattr(bundle_phase, "build_yaml_bundle", _slow_bundle)
    job = _make_remote_job()

    task = asyncio.get_running_loop().create_task(
        bundle_phase.run_bundle_phase(controller, job, asyncio.Event())
    )
    for _ in range(200):
        await asyncio.sleep(0.01)
        if any("still building bundle" in line for line in job.output):
            break
    release.set()

    assert await task == b"FAKEBUNDLE"
    assert any("still building bundle (" in line for line in job.output)
