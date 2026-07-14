"""Contract tests for the neutral sweep-loop base."""

from __future__ import annotations

from typing import Any

import pytest

from esphome_device_builder.controllers._device_state_monitor._sweep_source import SweepSource

from .conftest import make_state_monitor_with_callbacks


async def test_sweep_source_base_defaults() -> None:
    """The base runs unconditionally by default and demands a ``_sweep``."""
    monitor, _ = make_state_monitor_with_callbacks([])
    base = SweepSource(monitor)

    assert await base._prepare() is True
    with pytest.raises(NotImplementedError):
        await base._sweep()


async def test_idle_returns_immediately_when_woken() -> None:
    """A set wake event short-circuits the idle wait."""
    monitor, _ = make_state_monitor_with_callbacks([])
    base = SweepSource(monitor)
    base._wake.set()
    await base._idle()


async def test_idle_times_out_when_not_woken(monkeypatch: Any) -> None:
    """With no wake, the idle wait expires after the interval and returns."""
    monkeypatch.setattr(SweepSource, "_interval", 0.01)
    monitor, _ = make_state_monitor_with_callbacks([])
    await SweepSource(monitor)._idle()
