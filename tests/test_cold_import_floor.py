"""Lock in the heavy esphome subpackages that must stay cold at idle.

These upstream modules are heavy and now load only when the corresponding
feature is exercised:

- ``esphome.components.dashboard_import`` (~14 MB) — only used by
  the device-adoption WS command.
- ``esphome.bundle`` (~1 MB) — only used by the peer-link receiver
  when an offload submission lands.
- ``esphome.components.esp32`` / ``esphome.espidf`` — the esp32 package
  drags espidf -> requests -> esphome.config; platform metadata now comes
  from the generated ``platform_capabilities.index.json`` instead.
- ``esphome.components.wifi`` — native-wifi inference reads the same index.

This test runs the dashboard's import + ``start()`` path in a fresh
subprocess and asserts the modules stay out of ``sys.modules``. A future
module-level re-import trips the assertion and surfaces the regression in CI
(quiet +MB at idle, and seconds of startup on a slow SBC for the esp32 chain).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

_COLD_MODULES = (
    "esphome.components.dashboard_import",
    "esphome.bundle",
    "esphome.components.esp32",
    "esphome.components.wifi",
    "esphome.espidf",
)


def test_cold_modules_absent_after_start() -> None:
    """A fresh ``DeviceBuilder.start()`` does not load any cold-path esphome subpackage."""
    script = textwrap.dedent(
        """
        import asyncio
        import socket
        import sys
        import tempfile
        from pathlib import Path

        from esphome.core import CORE
        tmp = Path(tempfile.mkdtemp())
        CORE.config_path = tmp / "dashboard.yaml"

        from esphome_device_builder.controllers.config import DashboardSettings
        from esphome_device_builder.device_builder import DeviceBuilder

        # Pin both listener ports to OS-allocated free slots so a
        # parallel test run (or another local process) holding the
        # defaults can't make ``start()`` short-circuit before it
        # reaches the code paths the cold-module assertion is
        # guarding.
        def _free_port() -> int:
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                return s.getsockname()[1]

        settings = DashboardSettings(
            config_dir=tmp,
            port=_free_port(),
            remote_build_port=_free_port(),
        )

        started = False

        async def go() -> None:
            global started
            db = DeviceBuilder(settings)
            await db.start()
            started = True

        asyncio.run(go())
        assert started, "DeviceBuilder.start did not finish — cold-module check would be vacuous"

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
