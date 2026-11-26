"""Tests for ``--mcp-token-file`` loading and the MCP bearer compare in ``DashboardSettings``."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from esphome_device_builder.controllers.config import DashboardSettings


def _parse(tmp_path: Path, token_file: Path | None) -> DashboardSettings:
    settings = DashboardSettings()
    args = SimpleNamespace(
        configuration=str(tmp_path / "config"),
        mcp_token_file=None if token_file is None else str(token_file),
    )
    with patch.dict(os.environ, {}, clear=True):
        settings.parse_args(args)
    return settings


def test_missing_file_is_minted_and_reused_on_the_next_start(tmp_path: Path) -> None:
    token_file = tmp_path / "mcp_token"
    token = _parse(tmp_path, token_file).mcp_token
    assert len(token) >= 32
    assert token_file.read_text(encoding="utf-8") == token
    assert _parse(tmp_path, token_file).mcp_token == token


@pytest.mark.skipif(sys.platform == "win32", reason="Windows doesn't honor POSIX mode bits")
def test_minted_file_is_owner_only(tmp_path: Path) -> None:
    token_file = tmp_path / "mcp_token"
    _parse(tmp_path, token_file)
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


def test_existing_file_is_read_without_its_trailing_newline(tmp_path: Path) -> None:
    token_file = tmp_path / "mcp_token"
    token_file.write_text("existing-token\n", encoding="utf-8")
    assert _parse(tmp_path, token_file).mcp_token == "existing-token"
    assert token_file.read_text(encoding="utf-8") == "existing-token\n"


@pytest.mark.parametrize("content", ["", "\n"])
def test_empty_file_gets_a_minted_token(tmp_path: Path, content: str) -> None:
    token_file = tmp_path / "mcp_token"
    token_file.write_text(content, encoding="utf-8")
    token = _parse(tmp_path, token_file).mcp_token
    assert token
    assert token_file.read_text(encoding="utf-8") == token


def test_no_flag_leaves_the_token_unset(tmp_path: Path) -> None:
    assert _parse(tmp_path, None).mcp_token == ""


def test_check_mcp_token() -> None:
    settings = DashboardSettings(mcp_token="right")
    assert settings.check_mcp_token("right")
    assert not settings.check_mcp_token("wrong")
    # A surrogate-escaped header byte compares unequal instead of raising.
    assert not settings.check_mcp_token("right\udcff")
    assert not DashboardSettings().check_mcp_token("")
