"""Monitor-bound adapter over the shared presence-gated loop."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...helpers.presence_gated_loop import PresenceGatedLoop

if TYPE_CHECKING:
    from .controller import DeviceStateMonitor


class SweepSource(PresenceGatedLoop[None]):
    """Presence-gated fixed-interval sweep loop; subclasses supply ``_sweep``."""

    def __init__(self, monitor: DeviceStateMonitor) -> None:
        super().__init__(monitor.presence)
        self._monitor = monitor

    async def _work(self) -> None:
        await self._sweep()

    async def _sweep(self) -> None:
        raise NotImplementedError
