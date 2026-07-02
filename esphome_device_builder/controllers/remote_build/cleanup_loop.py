"""Receiver-side periodic cleanup sweep for cold remote-build subtrees."""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import TYPE_CHECKING

from ...helpers.async_ import run_in_executor
from ...helpers.remote_build_cleanup import sweep_remote_builds
from ...helpers.remote_build_layout import parse_from_configuration

if TYPE_CHECKING:
    from .receiver import ReceiverController

_LOGGER = logging.getLogger(__name__)

# Cleanup-sweep cadence — TTL itself is the
# operator-tunable knob (:data:`DEFAULT_CLEANUP_TTL_SECONDS`).
_CLEANUP_SWEEP_INTERVAL_SECONDS = 60 * 60


async def run_cleanup_loop(controller: ReceiverController) -> None:
    """Sweep cold remote-build subtrees every ``_CLEANUP_SWEEP_INTERVAL_SECONDS``.

    Sleeps before the first cycle — a fresh install has no
    subtrees to reclaim and the TTL is 24h. Per-cycle
    failures are logged and the loop continues; cancel via
    :meth:`ReceiverController.stop` settles cleanly through
    the sleep.
    """
    config_dir = controller._db.settings.config_dir
    while True:
        await asyncio.sleep(_CLEANUP_SWEEP_INTERVAL_SECONDS)
        try:
            # Re-check firmware narrows the type for mypy and
            # survives a future spawn/start decoupling.
            firmware = controller._db.firmware
            if firmware is None:
                continue
            settings = await controller._load_settings_async()
            in_flight_keys = frozenset(
                rbp
                for job in firmware.active_remote_peer_jobs()
                if (rbp := parse_from_configuration(job.configuration)) is not None
            )
            deleted = await run_in_executor(
                partial(
                    sweep_remote_builds,
                    config_dir,
                    ttl_seconds=settings.cleanup_ttl_seconds,
                    in_flight_keys=in_flight_keys,
                ),
            )
            if deleted:
                _LOGGER.info("remote-build cleanup: swept %d cold subtree(s)", deleted)
        except Exception:
            _LOGGER.exception("remote-build cleanup sweep failed")
