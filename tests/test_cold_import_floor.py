"""Lock in the heavy esphome subpackages that must stay cold at idle.

Two upstream modules are several MB each and now load only when
the corresponding feature is exercised:

- ``esphome.components.dashboard_import`` (~14 MB) — only used by
  the device-adoption WS command.
- ``esphome.bundle`` (~1 MB) — only used by the peer-link receiver
  when an offload submission lands.

This test runs the dashboard's import + ``start()`` path in a fresh
subprocess and asserts both modules stay out of ``sys.modules``. A
future module-level re-import trips the assertion and surfaces the
regression in CI rather than as quiet +15 MB on every HA addon idle.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

_COLD_MODULES = (
    "esphome.components.dashboard_import",
    "esphome.bundle",
)


def test_cold_modules_absent_after_start() -> None:
    """A fresh ``DeviceBuilder.start()`` does not load any cold-path esphome subpackage."""
    script = textwrap.dedent(
        """
        import asyncio
        import sys
        import tempfile
        from pathlib import Path

        from esphome.core import CORE
        tmp = Path(tempfile.mkdtemp())
        CORE.config_path = tmp / "dashboard.yaml"

        from esphome_device_builder.controllers.config import DashboardSettings
        from esphome_device_builder.device_builder import DeviceBuilder

        settings = DashboardSettings(config_dir=tmp)

        async def go() -> None:
            db = DeviceBuilder(settings)
            try:
                await db.start()
            finally:
                # ``DeviceBuilder.stop`` would do a cleaner teardown
                # but is not the target of the assertion below.
                pass

        try:
            asyncio.run(go())
        except OSError:
            # ``DeviceBuilder.start`` may fail to bind the
            # peer-link receiver socket if the port is taken; the
            # cold-modules assertion below is still meaningful.
            pass

        for name in %r:
            assert name not in sys.modules, name
        """
    ) % (_COLD_MODULES,)

    result = subprocess.run(  # noqa: S603 — script is fully test-controlled
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"cold-import regression\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
