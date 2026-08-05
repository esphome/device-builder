"""Behaviour of the cold-start shallow-seed refine pass."""

from __future__ import annotations

from types import SimpleNamespace

from esphome_device_builder.controllers.devices.refine import refine_shallow_scan
from tests._recording_scanner import RecordingScanner
from tests.conftest import make_device


async def test_refine_requests_every_device_then_drains() -> None:
    """Every seeded configuration is requested, then the pass parks on the drain."""
    scanner = RecordingScanner()
    scanner.devices = [make_device(name="kitchen"), make_device(name="porch")]
    controller = SimpleNamespace(_scanner=scanner)

    await refine_shallow_scan(controller)

    assert ("request", "kitchen.yaml") in scanner.calls
    assert ("request", "porch.yaml") in scanner.calls
    assert scanner.calls[-1] == ("wait_idle",)
