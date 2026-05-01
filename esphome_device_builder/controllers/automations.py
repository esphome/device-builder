"""Automations controller — triggers and actions for device automation.

Provides context-aware listing of available triggers and actions based on
what components are configured in a device.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mashumaro.mixins.orjson import DataClassORJSONMixin

from ..helpers.api import api_command

if TYPE_CHECKING:
    from ..device_builder import DeviceBuilder

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass
class AutomationTrigger(DataClassORJSONMixin):
    """A trigger that can start an automation."""

    id: str  # e.g. "on_press"
    name: str  # e.g. "On Press"
    description: str
    platform_types: list[str] = field(default_factory=list)  # e.g. ["binary_sensor", "button"]


@dataclass
class AutomationAction(DataClassORJSONMixin):
    """An action that can be performed in an automation."""

    id: str  # e.g. "switch.turn_on"
    name: str  # e.g. "Turn Switch On"
    description: str


# ---------------------------------------------------------------------------
# Built-in trigger/action definitions
# ---------------------------------------------------------------------------

# Device-level triggers (available on any device)
_DEVICE_TRIGGERS: list[AutomationTrigger] = [
    AutomationTrigger(
        id="on_boot",
        name="On Boot",
        description="Fires when the device starts up.",
        platform_types=[],
    ),
    AutomationTrigger(
        id="on_shutdown",
        name="On Shutdown",
        description="Fires when the device is shutting down.",
        platform_types=[],
    ),
    AutomationTrigger(
        id="on_loop",
        name="On Loop",
        description="Fires on every main loop iteration.",
        platform_types=[],
    ),
]

# Component-level triggers (available when the matching component type is present)
_COMPONENT_TRIGGERS: list[AutomationTrigger] = [
    # Binary sensor triggers
    AutomationTrigger(
        id="on_press",
        name="On Press",
        description="Fires when a binary sensor transitions to ON.",
        platform_types=["binary_sensor", "button"],
    ),
    AutomationTrigger(
        id="on_release",
        name="On Release",
        description="Fires when a binary sensor transitions to OFF.",
        platform_types=["binary_sensor"],
    ),
    AutomationTrigger(
        id="on_click",
        name="On Click",
        description="Fires on a short click (press + release within a time window).",
        platform_types=["binary_sensor"],
    ),
    AutomationTrigger(
        id="on_double_click",
        name="On Double Click",
        description="Fires when a binary sensor is clicked twice quickly.",
        platform_types=["binary_sensor"],
    ),
    AutomationTrigger(
        id="on_state",
        name="On State Change",
        description="Fires whenever the component state changes.",
        platform_types=["binary_sensor", "switch", "sensor", "text_sensor"],
    ),
    # Sensor triggers
    AutomationTrigger(
        id="on_value",
        name="On Value",
        description="Fires when a sensor publishes a new value.",
        platform_types=["sensor"],
    ),
    AutomationTrigger(
        id="on_value_range",
        name="On Value Range",
        description="Fires when a sensor value enters or leaves a range.",
        platform_types=["sensor"],
    ),
    # Switch/light/fan triggers
    AutomationTrigger(
        id="on_turn_on",
        name="On Turn On",
        description="Fires when the device is turned on.",
        platform_types=["switch", "light", "fan"],
    ),
    AutomationTrigger(
        id="on_turn_off",
        name="On Turn Off",
        description="Fires when the device is turned off.",
        platform_types=["switch", "light", "fan"],
    ),
]

# Common actions
_ACTIONS: list[AutomationAction] = [
    AutomationAction(
        id="switch.toggle",
        name="Toggle Switch",
        description="Toggle a switch between on and off.",
    ),
    AutomationAction(
        id="switch.turn_on",
        name="Turn Switch On",
        description="Turn a switch on.",
    ),
    AutomationAction(
        id="switch.turn_off",
        name="Turn Switch Off",
        description="Turn a switch off.",
    ),
    AutomationAction(
        id="light.turn_on",
        name="Turn Light On",
        description="Turn a light on, optionally with brightness/colour.",
    ),
    AutomationAction(
        id="light.turn_off",
        name="Turn Light Off",
        description="Turn a light off.",
    ),
    AutomationAction(
        id="delay",
        name="Delay",
        description="Wait for a specified duration before continuing.",
    ),
    AutomationAction(
        id="logger.log",
        name="Log Message",
        description="Print a message to the ESPHome log.",
    ),
    AutomationAction(
        id="lambda",
        name="Lambda (Custom Code)",
        description="Execute custom C++ code.",
    ),
]


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class AutomationsController:
    """Provides context-aware automation triggers and actions."""

    def __init__(self, device_builder: DeviceBuilder) -> None:
        self._db = device_builder

    @api_command("automations/get_triggers")
    async def get_triggers(
        self,
        *,
        platform_type: str | None = None,
        **kwargs: Any,
    ) -> list[dict]:
        """Get available triggers, optionally filtered by platform type.

        If platform_type is given (e.g. "binary_sensor"), returns only triggers
        applicable to that type. Otherwise returns all triggers.
        """
        if platform_type:
            return [
                t.to_dict()
                for t in _DEVICE_TRIGGERS + _COMPONENT_TRIGGERS
                if not t.platform_types or platform_type in t.platform_types
            ]
        return [t.to_dict() for t in _DEVICE_TRIGGERS + _COMPONENT_TRIGGERS]

    @api_command("automations/get_actions")
    async def get_actions(self, **kwargs: Any) -> list[dict]:
        """Get all available automation actions."""
        return [a.to_dict() for a in _ACTIONS]

    @api_command("automations/get_available")
    async def get_available_for_device(
        self,
        *,
        configuration: str,
        **kwargs: Any,
    ) -> dict:
        """Get all triggers and actions available for a specific device config.

        Reads the device config to determine which component types are present,
        then returns applicable triggers + all actions.
        """
        # Read the device config to find which component types are in use
        config_path = self._db.settings.rel_path(configuration)
        loop = asyncio.get_running_loop()
        content = await loop.run_in_executor(None, config_path.read_text, "utf-8")

        # Simple YAML key detection — find top-level keys that match platform types
        present_types: set[str] = set()
        for line in content.splitlines():
            if line and not line[0].isspace() and ":" in line:
                key = line.split(":")[0].strip()
                if key in _PLATFORM_TYPES:
                    present_types.add(key)

        # Device-level triggers always available
        triggers = list(_DEVICE_TRIGGERS)

        # Add component-level triggers matching present platform types
        triggers.extend(
            trigger
            for trigger in _COMPONENT_TRIGGERS
            if any(pt in present_types for pt in trigger.platform_types)
        )

        return {
            "triggers": [t.to_dict() for t in triggers],
            "actions": [a.to_dict() for a in _ACTIONS],
            "present_platform_types": sorted(present_types),
        }


# Platform types that can have automation triggers
_PLATFORM_TYPES = {
    "binary_sensor",
    "sensor",
    "switch",
    "light",
    "fan",
    "cover",
    "climate",
    "button",
    "number",
    "text_sensor",
}
