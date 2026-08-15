"""Resolve sibling CLI commands anchored on ``sys.executable``."""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path


def helper_cli_cmd() -> tuple[str, ...]:
    """Argv prefix for the ``device-builder-helper`` child.

    One spelling of the name/module pair, so the download-types and
    decode-backtrace callers can't resolve the helper differently.
    """
    return _find_sibling_cli("device-builder-helper", "esphome_device_builder.helper_cli")


def _find_esphome_cmd() -> list[str]:
    """Locate the ``esphome`` CLI, preferring the same interpreter as ours.

    The backend's own interpreter (``sys.executable``) is the
    authoritative source: if it can import ``esphome`` to start the
    server, it can run ``python -m esphome`` for compile jobs. We
    don't try to substitute a sibling ``python`` next to
    ``sys.executable`` — that's an easy way to silently jump to a
    different interpreter (e.g. a system Python without esphome
    installed) and produce confusing "No module named esphome"
    errors at compile time.

    A standalone ``esphome`` script in the *same* bin directory as
    our interpreter is preferred when present (slightly cheaper than
    ``python -m esphome`` and surfaces a friendlier traceback when
    something goes wrong inside esphome).
    """
    return list(_find_sibling_cli("esphome"))


def _find_esptool_cmd() -> list[str]:
    """Locate the ``esptool`` CLI, preferring the same interpreter as ours.

    Same sibling-script-first lookup as :func:`_find_esphome_cmd`.
    The sibling script's shebang is pinned to our interpreter so it
    can't accidentally jump to a different Python — and it dodges
    the ``"No module named esptool"`` failure mode under VS Code's
    debugpy launch chain, where ``python -m esptool`` from inside
    a debug-wrapped process can fail module resolution in ways the
    parent process doesn't.
    """
    return list(_find_sibling_cli("esptool"))


@lru_cache(maxsize=8)
def _find_sibling_cli(name: str, module: str | None = None) -> tuple[str, ...]:
    """Sibling script next to ``sys.executable``, else ``python -m <module or name>``.

    *module* lets the ``-m`` fallback target an import path that differs from the
    console-script *name* (e.g. ``device-builder-helper`` ->
    ``esphome_device_builder.helper_cli``); it defaults to *name*.

    Result is cached so the ``sibling.exists()`` filesystem probe
    runs once per ``name`` — async callers (``_run_esptool``,
    ``verify_chip``) would otherwise trip ``blockbuster`` on every
    invocation, since ``Path.exists`` calls ``os.stat`` synchronously.

    Returns a tuple so the cached value can't be mutated by callers
    that copy it into their own argv list.
    """
    python = sys.executable
    sibling = Path(python).parent / (f"{name}.exe" if os.name == "nt" else name)
    if sibling.exists():
        return (str(sibling),)
    return (python, "-m", module or name)
