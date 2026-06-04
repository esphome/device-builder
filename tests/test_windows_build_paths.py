"""Unit contract for the Windows short-build-paths helper (the no-op side runs everywhere)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from esphome_device_builder.helpers.windows_build_paths import windows_short_build_paths


@pytest.mark.skipif(sys.platform == "win32", reason="pins the off-Windows no-op contract")
def test_context_manager_is_noop_off_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off Windows the context manager touches nothing and yields ``None``."""
    monkeypatch.delenv("ESPHOME_DATA_DIR", raising=False)
    with windows_short_build_paths(tmp_path) as pio_core_dir:
        assert pio_core_dir is None
        assert "ESPHOME_DATA_DIR" not in os.environ
    assert "ESPHOME_DATA_DIR" not in os.environ
