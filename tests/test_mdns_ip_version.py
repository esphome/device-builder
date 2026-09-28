"""Regression test for the mDNS responder's IP version."""

from __future__ import annotations

from typing import Any

import pytest
from zeroconf import IPVersion

from esphome_device_builder.controllers._device_state_monitor import mdns as mdns_module

from .conftest import make_state_monitor_with_callbacks


async def test_start_binds_ipv4_and_ipv6(monkeypatch: pytest.MonkeyPatch) -> None:
    """The responder is created with ``IPVersion.All``."""
    captured: dict[str, Any] = {}

    def _fake_zeroconf(**kwargs: Any) -> None:
        captured.update(kwargs)
        raise RuntimeError("stop after construction")

    monkeypatch.setattr(mdns_module, "AsyncEsphomeZeroconf", _fake_zeroconf)
    monitor, _callbacks = make_state_monitor_with_callbacks([])

    await monitor.mdns.start()

    assert captured == {"ip_version": IPVersion.All}
