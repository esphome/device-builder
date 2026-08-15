"""Every ``helpers.*`` module must import standalone, first, in a fresh process."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import esphome_device_builder.helpers as helpers_pkg


def _helper_modules() -> list[str]:
    """Dotted names of every module under ``esphome_device_builder.helpers``."""
    root = Path(helpers_pkg.__file__).parent
    modules: list[str] = []
    for py in sorted(root.rglob("*.py")):
        parts = list(py.relative_to(root.parent).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules.append(".".join(["esphome_device_builder", *parts]))
    return modules


def test_helpers_import_standalone() -> None:
    """No helper module depends on ``controllers.*`` (or anything else) importing first.

    A ``helpers`` module that imports ``controllers.*`` can form a
    cycle that only surfaces when the helper is imported before the
    controller package — ``helpers.config_bundle`` hit exactly this
    via ``controllers.firmware.remote_runner``. Each module is
    imported into a purged ``sys.modules`` so it is always the very
    first package module loaded, in one subprocess.
    """
    modules = _helper_modules()
    assert "esphome_device_builder.helpers.config_bundle" in modules
    script = textwrap.dedent(
        """
        import importlib
        import sys
        import traceback

        failures = []
        for mod in %r:
            stale = [
                name
                for name in sys.modules
                if name == "esphome_device_builder"
                or name.startswith("esphome_device_builder.")
            ]
            for name in stale:
                del sys.modules[name]
            try:
                importlib.import_module(mod)
            except Exception:
                failures.append(mod + "\\n" + traceback.format_exc())
        if failures:
            sys.stderr.write("\\n\\n".join(failures))
            sys.exit(1)
        """
    ) % (modules,)

    result = subprocess.run(  # noqa: S603 — script is fully test-controlled
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, (
        f"helper module not standalone-importable\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
