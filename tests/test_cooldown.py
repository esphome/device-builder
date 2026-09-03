"""Tests for the shared cooldown ledger."""

from __future__ import annotations

from esphome_device_builder.helpers.cooldown import CooldownLedger


def test_escalate_survives_unbounded_strikes() -> None:
    """The exponent clamp keeps a years-long strike streak finite."""
    cooldowns: CooldownLedger[str] = CooldownLedger()
    for _ in range(5000):
        cooldowns.escalate("alpha", 300.0, 3600.0)
    assert cooldowns.remaining("alpha") <= 3600.0
    assert cooldowns.strikes("alpha") == 5000


def test_discard_and_clear_drop_deadlines_and_strikes() -> None:
    cooldowns: CooldownLedger[str] = CooldownLedger()
    cooldowns.escalate("alpha", 10.0, 100.0)
    cooldowns.escalate("beta", 10.0, 100.0)
    cooldowns.discard("alpha")
    assert "alpha" not in cooldowns
    assert cooldowns.strikes("alpha") == 0
    cooldowns.clear()
    assert "beta" not in cooldowns
