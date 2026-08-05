"""Background deep-reload of the shallow cold-start seed."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .controller import DevicesController

_LOGGER = logging.getLogger(__name__)


async def refine_shallow_scan(controller: DevicesController) -> None:
    """Deep-reload every shallow-seeded device through the scanner's drain worker."""
    for device in controller._scanner.devices:
        controller._scanner.request(device.configuration)
    await controller._scanner.wait_idle()
    _LOGGER.debug("Cold-start refine complete — %d devices", len(controller._scanner.devices))
