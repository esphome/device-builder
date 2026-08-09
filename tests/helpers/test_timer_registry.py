"""Coverage for ``helpers.timer_registry.TimerRegistry``."""

from __future__ import annotations

from esphome_device_builder.helpers.timer_registry import TimerRegistry
from tests.conftest import make_timer_handle


async def test_arm_installs_handle() -> None:
    registry: TimerRegistry[str] = TimerRegistry()
    handle = make_timer_handle()

    registry.arm("a.yaml", handle)

    assert "a.yaml" in registry
    assert len(registry) == 1
    assert registry["a.yaml"] is handle
    registry.cancel_all()


async def test_arm_replaces_and_cancels_prior_handle() -> None:
    registry: TimerRegistry[str] = TimerRegistry()
    first = make_timer_handle()
    second = make_timer_handle()

    registry.arm("a.yaml", first)
    registry.arm("a.yaml", second)

    assert first.cancelled()
    assert not second.cancelled()
    assert registry["a.yaml"] is second
    assert len(registry) == 1
    registry.cancel_all()


async def test_discard_cancels_and_removes() -> None:
    registry: TimerRegistry[str] = TimerRegistry()
    handle = make_timer_handle()
    registry.arm("a.yaml", handle)

    registry.discard("a.yaml")

    assert handle.cancelled()
    assert "a.yaml" not in registry
    assert len(registry) == 0


async def test_discard_missing_key_is_a_noop() -> None:
    registry: TimerRegistry[str] = TimerRegistry()

    registry.discard("missing.yaml")

    assert len(registry) == 0


async def test_cancel_all_cancels_and_returns_held_keys() -> None:
    registry: TimerRegistry[str] = TimerRegistry()
    handles = {"a.yaml": make_timer_handle(), "b.yaml": make_timer_handle()}
    for key, handle in handles.items():
        registry.arm(key, handle)

    cancelled = registry.cancel_all()

    assert sorted(cancelled) == ["a.yaml", "b.yaml"]
    assert all(handle.cancelled() for handle in handles.values())
    assert len(registry) == 0
