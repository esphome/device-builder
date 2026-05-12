"""Shared fixtures for the CodSpeed benchmark suite.

Mirrors the ``aioesphomeapi`` pattern: silence the package's debug
logging so the benchmarks measure the streaming hot path itself,
not the cost of the formatter / handlers we'd otherwise pay for
every chunk.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _silence_debug_logging() -> object:
    """Pin ``esphome_device_builder`` logger at WARNING during benchmarks.

    Debug logging in the streaming path (``firmware.py``,
    ``_device_state_monitor.py``, ``controllers/devices.py``) issues
    a ``_LOGGER.debug`` per chunk in the worst case. With Python's
    default debug-disabled loggers that's a single ``isEnabledFor``
    check, but turning it on under instrumentation skews CodSpeed
    numbers without measuring anything we care about.
    """
    logger = logging.getLogger("esphome_device_builder")
    original_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        logger.setLevel(original_level)


@pytest.fixture(autouse=True)
def blockbuster() -> Iterator[None]:
    """Disable the blockbuster guard for benchmarks.

    The package-wide ``tests/conftest.py`` enables blockbuster on
    Linux CI to catch blocking syscalls inside the event loop on
    request paths. Benchmarks that stand up real
    :class:`DeviceBuilder` instances (the 128 MiB paired
    round-trip) hit one-time sync I/O during ``start()``
    (boards.json + components.json catalog loads); allowlisting
    every frame would defeat the purpose, and the benchmarks
    aren't request-path coverage anyway. Override the autouse to
    yield without entering the blockbuster context.
    """
    yield None
