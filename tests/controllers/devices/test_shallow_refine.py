"""Behaviour of the cold-start shallow-seed refine pass."""

from __future__ import annotations

from types import SimpleNamespace

from esphome_device_builder.controllers.devices.refine import refine_shallow_scan
from tests._recording_scanner import RecordingScanner
from tests.conftest import make_device


class _RecordingCoordinator:
    """Records reconcile calls into the shared scanner call log."""

    def __init__(self, calls: list[tuple[object, ...]]) -> None:
        self._calls = calls

    async def reconcile(self) -> None:
        self._calls.append(("reconcile",))


async def test_refine_requests_every_device_then_reconciles() -> None:
    """Every seeded configuration is requested, then reconcile runs after the drain."""
    scanner = RecordingScanner()
    scanner.devices = [make_device(name="kitchen"), make_device(name="porch")]
    controller = SimpleNamespace(
        _scanner=scanner,
        _mqtt_coordinator=_RecordingCoordinator(scanner.calls),
    )

    await refine_shallow_scan(controller)

    assert ("request", "kitchen.yaml") in scanner.calls
    assert ("request", "porch.yaml") in scanner.calls
    assert scanner.calls[-2:] == [("wait_idle",), ("reconcile",)]
