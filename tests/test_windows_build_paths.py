"""Unit contract for the Windows short-build-paths helper (the no-op side runs everywhere)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from esphome_device_builder.helpers.windows_build_paths import (
    apply_windows_short_build_paths,
    remove_windows_short_build_paths,
    windows_pio_core_dir,
)


@pytest.mark.skipif(sys.platform == "win32", reason="pins the off-Windows no-op contract")
def test_apply_is_noop_off_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Off Windows the helper touches nothing: no env override, no recorded core dir."""
    monkeypatch.delenv("ESPHOME_DATA_DIR", raising=False)
    apply_windows_short_build_paths(tmp_path)
    assert "ESPHOME_DATA_DIR" not in os.environ
    assert windows_pio_core_dir() is None
    remove_windows_short_build_paths()  # also a no-op; must not raise
    assert windows_pio_core_dir() is None
