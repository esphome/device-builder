"""Background deep-reload of the shallow cold-start seed."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .controller import DevicesController

_LOGGER = logging.getLogger(__name__)


async def refine_shallow_scan(controller: DevicesController) -> None:
    """Deep-reload every shallow-seeded device through the scanner's drain worker."""
    scanner = controller._scanner
    for device in scanner.devices:
        scanner.request(device.configuration)
    await scanner.wait_idle()
    if retry := scanner.poisoned_configurations():
        # One bounded retry catches transient failures without waiting
        # on a scan a headless install may never run; persistent ones
        # stay poisoned (and warned) for the next scan.
        for configuration in retry:
            scanner.request(configuration)
        await scanner.wait_idle()
    if remaining := scanner.poisoned_configurations():
        _LOGGER.warning(
            "Cold-start refine left %d device(s) shallow: %s", len(remaining), remaining
        )
    _LOGGER.debug("Cold-start refine complete — %d devices", len(scanner.devices))
