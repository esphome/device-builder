"""Tests for ``helpers.startup_timing.StartupTimer`` and its wiring."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from esphome_device_builder.device_builder import DeviceBuilder
from esphome_device_builder.helpers.startup_timing import StartupTimer

from .conftest import MakeSettingsFactory


def _timer(ticks: list[float]) -> StartupTimer:
    """Build a timer whose clock yields ``ticks`` in order; origin is ``ticks[0]``."""
    it = iter(ticks)
    origin = next(it)
    return StartupTimer(origin, clock=lambda: next(it))


def test_marks_accumulate_phase_deltas() -> None:
    timer = _timer([100.0, 109.4, 109.6, 109.7, 112.7])
    assert timer.mark("import") == pytest.approx(9.4)
    assert timer.mark("settings") == pytest.approx(0.2)
    assert timer.mark("app") == pytest.approx(0.1)
    assert timer.mark("controllers") == pytest.approx(3.0)


def test_total_is_origin_to_last_mark() -> None:
    timer = _timer([100.0, 109.4, 112.7])
    timer.mark("import")
    timer.mark("controllers")
    assert timer.total == pytest.approx(12.7)


def test_summary_format() -> None:
    timer = _timer([0.0, 9.4, 12.4])
    timer.mark("import")
    timer.mark("controllers")
    assert timer.summary() == "total=12.4s (import=9.4s controllers=3.0s)"


def test_summary_total_is_sum_of_rounded_parts() -> None:
    # Many sub-0.1s phases: the printed total must equal the sum of the printed
    # parts, not the independently-rounded origin-to-last span.
    timer = _timer([0.0, 0.04, 0.08, 0.12, 0.16])
    for name in ("a", "b", "c", "d"):
        timer.mark(name)
    assert timer.summary() == "total=0.0s (a=0.0s b=0.0s c=0.0s d=0.0s)"


def _stub_run(db: DeviceBuilder) -> None:
    """Run the server with ``web.run_app`` stubbed so it returns immediately."""
    with patch("esphome_device_builder.device_builder.web.run_app", lambda *a, **k: None):
        db.run()


def test_run_marks_app_phase_on_normal_path(make_settings: MakeSettingsFactory) -> None:
    timer = StartupTimer(0.0)
    settings = make_settings()
    settings.on_ha_addon = True
    settings.using_password = True
    settings.username = "admin"
    settings.password_hash = b"x" * 32
    settings.host = "0.0.0.0"
    settings.port = 6052
    _stub_run(DeviceBuilder(settings, startup_timer=timer))
    assert "app=" in timer.summary()


def test_run_marks_app_phase_on_ingress_only_path(
    make_settings: MakeSettingsFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DISABLE_HA_AUTHENTICATION", raising=False)
    timer = StartupTimer(0.0)
    settings = make_settings()
    settings.on_ha_addon = True
    settings.using_password = False
    settings.host = "0.0.0.0"
    settings.port = 6052
    settings.ingress_port = 6053
    settings.ingress_host = ""
    _stub_run(DeviceBuilder(settings, startup_timer=timer))
    assert "app=" in timer.summary()


def test_run_marks_app_phase_on_front_door_open_path(
    make_settings: MakeSettingsFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISABLE_HA_AUTHENTICATION", "true")
    timer = StartupTimer(0.0)
    settings = make_settings()
    settings.on_ha_addon = True
    settings.using_password = False
    settings.allow_public_port = True
    settings.host = "0.0.0.0"
    settings.port = 6052
    settings.ingress_port = 6053
    settings.ingress_host = ""
    _stub_run(DeviceBuilder(settings, startup_timer=timer))
    assert "app=" in timer.summary()


async def test_start_marks_controllers_and_logs_summary(
    make_settings: MakeSettingsFactory,
    caplog: pytest.LogCaptureFixture,
    _hermetic_lifecycle: None,
) -> None:
    timer = StartupTimer(0.0)
    db = DeviceBuilder(make_settings(with_core_path=True), startup_timer=timer)
    try:
        with caplog.at_level(logging.INFO, logger="esphome_device_builder.device_builder"):
            await db.start()
        assert "controllers=" in timer.summary()
        assert any("Startup phases" in rec.getMessage() for rec in caplog.records)
    finally:
        await db.stop()
