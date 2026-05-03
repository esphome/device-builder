"""Tests for the synchronous ``EventBus`` core.

The bus is exercised heavily by every controller test that fires
events, but the *exception-isolation* branch — one listener
raising must not abort delivery to its peers — has no direct
coverage. Pin it: a noisy listener can't take the whole fan-out
down with it.
"""

from __future__ import annotations

import logging

import pytest

from esphome_device_builder.helpers.event_bus import Event, EventBus
from esphome_device_builder.models import EventType


def test_fire_logs_and_continues_when_listener_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A raising listener doesn't block delivery to the rest.

    The bus is the controllers' shared fan-out — a single buggy
    subscriber (a frontend stream that's gone, a coroutine
    callback misused as sync) shouldn't tank every other
    subscriber's event. Pin the swallow + log so a refactor that
    propagated the exception would surface here as the second
    listener never seeing the event.
    """
    bus = EventBus()
    captured: list[Event] = []

    def _explode(_event: Event) -> None:
        msg = "boom"
        raise RuntimeError(msg)

    def _record(event: Event) -> None:
        captured.append(event)

    bus.add_listener(EventType.JOB_OUTPUT, _explode)
    bus.add_listener(EventType.JOB_OUTPUT, _record)

    with caplog.at_level(logging.ERROR, logger="esphome_device_builder.helpers.event_bus"):
        bus.fire(EventType.JOB_OUTPUT, {"job_id": "j1", "line": "hi"})

    # The good listener still saw the event.
    assert len(captured) == 1
    assert captured[0].event_type is EventType.JOB_OUTPUT
    assert captured[0].data == {"job_id": "j1", "line": "hi"}
    # The exception is logged with traceback, not silently dropped.
    assert any(
        "Event listener raised an exception" in rec.message
        and rec.levelname == "ERROR"
        and rec.exc_info is not None
        for rec in caplog.records
    ), [rec.message for rec in caplog.records]
