"""
Build a self-contained ESPHome bundle from a YAML on disk.

Wraps :class:`esphome.bundle.ConfigBundleCreator` for the
offloader-side ``submit_job`` flow (issue #106 phase 5c-3): the
WS handler hands a YAML filename, this module returns the
bundle bytes ready for chunking onto the peer-link.

ConfigBundleCreator + the upstream ``read_config`` it depends on
both lean on global :data:`esphome.core.CORE` — ``CORE.config_path``,
``CORE.config_dir``, ``CORE.config``. The dashboard sets
``CORE.config_path`` to a sentinel YAML at startup
(:mod:`controllers.config`); building a real bundle needs a
temporary swap to the actual YAML path, so concurrent calls
would race the global. :func:`build_yaml_bundle` serialises every
call through a module-level :class:`asyncio.Lock` and runs the
blocking work in an executor — same pattern the legacy dashboard
used for its in-process compile-related helpers.

CORE state is restored in a ``finally`` so a downstream caller
that depends on the dashboard sentinel (``ext_storage_path``,
``CORE.data_dir``) sees the dashboard layout again as soon as
the bundle call returns.

Errors:

* :class:`FileNotFoundError` from missing YAML — propagated; the
  WS layer maps to ``CommandError(NOT_FOUND)``.
* :class:`esphome.core.EsphomeError` (raised by ``read_config``
  on schema-invalid YAML / missing includes / bad secrets) —
  propagated; the WS layer maps to ``CommandError(INVALID_ARGS)``
  with the message preserved for the user.
* Any other exception from inside ``create_bundle`` is
  propagated; the WS layer maps to ``CommandError(INTERNAL_ERROR)``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from esphome.bundle import ConfigBundleCreator
from esphome.config import read_config
from esphome.core import CORE

_LOGGER = logging.getLogger(__name__)

# Process-wide guard around ``CORE`` mutation. Every consumer
# of this module sees its bundle build serialised behind every
# other consumer's, but bundle creation is dominated by tar
# packing (sub-100ms on typical configs, seconds on big trees);
# the lock contention vs. CPU work ratio is low. Module-level
# rather than controller-level because :data:`CORE` is itself a
# module-level singleton — no value in scoping the lock more
# narrowly than the thing it's protecting.
_CORE_LOCK = asyncio.Lock()


async def build_yaml_bundle(yaml_path: Path) -> bytes:
    """Build a gzipped-tar bundle for *yaml_path* and return its raw bytes.

    Resolves *yaml_path*, validates the YAML through ESPHome's
    own ``read_config`` (catches schema errors, missing
    includes, malformed secrets), and packs the result + every
    referenced file into a single tarball via
    :class:`esphome.bundle.ConfigBundleCreator`.

    Serialised on a module-level lock — concurrent callers
    queue rather than race :data:`esphome.core.CORE` mutation.
    The blocking work (read_config + tar packing) runs in the
    default executor so the event loop stays responsive.
    """
    async with _CORE_LOCK:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _build_yaml_bundle_sync, yaml_path)


def _build_yaml_bundle_sync(yaml_path: Path) -> bytes:
    """Build the bundle synchronously; caller holds :data:`_CORE_LOCK`.

    Saves the dashboard's previous ``CORE.config_path`` /
    ``CORE.config`` and restores them in ``finally`` so a
    follow-up :func:`helpers.config_hash.read_build_info_hash`
    or any other CORE-dependent helper sees the same layout it
    saw before the bundle call.
    """
    if not yaml_path.is_file():
        msg = f"YAML not found: {yaml_path}"
        raise FileNotFoundError(msg)
    saved_config_path = CORE.config_path
    saved_config = CORE.config
    try:
        CORE.config_path = yaml_path
        config = read_config({})
        if config is None:
            msg = f"read_config returned None for {yaml_path} (validation failed)"
            raise RuntimeError(msg)
        CORE.config = config
        result = ConfigBundleCreator(config).create_bundle()
        # ``BundleResult.data`` is ``bytes`` — cast through the
        # method's untyped return so mypy sees the full chain.
        return bytes(result.data)
    finally:
        CORE.config_path = saved_config_path
        CORE.config = saved_config
