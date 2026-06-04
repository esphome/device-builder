r"""
Relocate Windows build data to one short, space-free root (dodges MAX_PATH + spaces).

Native Windows ESP-IDF builds fail two ways from a normal config path: the 260-char ``MAX_PATH``
limit on the deep build tree, and a pioarduino whitespace guard / gcc ``-fdebug-prefix-map``
truncation when the path contains a space (common: ``C:\Users\First Last\...``).

:func:`windows_short_build_paths` moves the whole build tree to ``C:\esphb-<id8>`` (short,
space-free, drive-root) for the ``with`` block: ``ESPHOME_DATA_DIR`` = that root (so
``CORE.data_dir`` resolves there for the dashboard's own reads *and* every compile subprocess)
and the yielded ``PLATFORMIO_CORE_DIR`` = ``<root>\pio``. Existing ``<config>/.esphome`` and
``~/.platformio`` are moved in once so warm caches survive. Real dirs (no junction), so CMake's
REALPATH can't reintroduce the original spaced/long path. The root lives outside the config dir
and is left on uninstall (so a reinstall keeps the warm toolchain); delete ``C:\esphb-*`` by hand
to reclaim the space. No-op off Windows -- including a Linux Docker container on Windows (the
gate is ``os.name == "nt"``), which keeps its normal data dir.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .dashboard_identity import get_or_create_dashboard_id

_LOGGER = logging.getLogger(__name__)

# Drive root the relocated data lives under; a module attribute so tests can repoint it.
_ROOT_BASE = Path("C:\\")
_DASHBOARD_ID_CHARS = 8


@contextmanager
def windows_short_build_paths(config_dir: Path) -> Iterator[Path | None]:
    """Relocate the build tree to a short space-free root for the block; yield the pio core dir.

    Off Windows (or if relocation can't run) yields ``None`` and changes nothing.
    """
    if not _is_windows():
        yield None
        return

    root = _ROOT_BASE / f"esphb-{get_or_create_dashboard_id(config_dir)[:_DASHBOARD_ID_CHARS]}"
    pio = root / "pio"
    prior = os.environ.get("ESPHOME_DATA_DIR")

    try:
        if not root.exists():
            _migrate(config_dir, root, pio)
        root.mkdir(parents=True, exist_ok=True)
        pio.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Logged at error so a later deep/spaced build failure traces back to this setup miss.
        _LOGGER.exception("Could not relocate Windows build data; deep/spaced builds may fail")
        yield None
        return

    os.environ["ESPHOME_DATA_DIR"] = str(root)
    _LOGGER.info("Windows build data relocated to %s (core %s)", root, pio)
    try:
        yield pio
    finally:
        if prior is None:
            os.environ.pop("ESPHOME_DATA_DIR", None)
        else:
            os.environ["ESPHOME_DATA_DIR"] = prior


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _is_windows() -> bool:
    """Whether relocation applies (a seam tests flip to drive the nt branch)."""
    return os.name == "nt"


def _platformio_dir() -> Path:
    """Default toolchain dir to migrate from (a seam; tests avoid the real ~/.platformio)."""
    return Path.home() / ".platformio"


def _migrate(config_dir: Path, root: Path, pio: Path) -> None:
    """Move existing build data + toolchain into the new root (one-time; root is new here).

    Same-volume moves are a fast rename; cross-volume falls back to copy. Moving ``~/.platformio``
    affects other PlatformIO/esphome installs sharing it (accepted: the toolchain is expensive to
    re-download and the desktop owns it).
    """
    old_data = config_dir / ".esphome"
    if old_data.is_dir():
        shutil.move(str(old_data), str(root))
    old_pio = _platformio_dir()
    if old_pio.is_dir() and not pio.exists():
        root.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old_pio), str(pio))
