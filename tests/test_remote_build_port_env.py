"""Tests for ``ESPHOME_REMOTE_BUILD_PORT`` env-var handling.

Mirror the precedence rules used by ``--trusted-domains`` /
``$ESPHOME_TRUSTED_DOMAINS`` and ``--username`` /
``$ESPHOME_USERNAME``: an explicit CLI value (any non-``None``)
wins over the env var; a ``None`` CLI default falls back to
``$ESPHOME_REMOTE_BUILD_PORT``; an empty / unset env var falls
back to ``DEFAULT_REMOTE_BUILD_PORT``; a non-integer env var
logs a warning and falls back to the default.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from esphome_device_builder.constants import DEFAULT_REMOTE_BUILD_PORT
from esphome_device_builder.controllers.config import DashboardSettings


def _ns(configuration: str, **kwargs: object) -> SimpleNamespace:
    """Minimal argparse-namespace stub for ``DashboardSettings.parse_args``."""
    defaults: dict[str, object] = {
        "ha_addon": False,
        "configuration": configuration,
        "username": "",
        "password": "",
        "log_level": "info",
        "port": 6052,
        "host": "0.0.0.0",
        "ingress_port": 6053,
        "ingress_host": "",
        # ``None`` matches the argparse default — production passes
        # ``None`` when ``--remote-build-port`` wasn't given so the
        # env-var fallback can fire.
        "remote_build_port": None,
        "dev": False,
        "trusted_domains": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_cli_flag_wins_over_env(tmp_path: Path) -> None:
    """Explicit ``--remote-build-port`` value beats the env var."""
    settings = DashboardSettings()
    with patch.dict("os.environ", {"ESPHOME_REMOTE_BUILD_PORT": "9999"}, clear=False):
        settings.parse_args(_ns(configuration=str(tmp_path), remote_build_port=7000))
    assert settings.remote_build_port == 7000


def test_env_used_when_cli_unset(tmp_path: Path) -> None:
    """``None`` CLI default falls back to the env var."""
    settings = DashboardSettings()
    with patch.dict("os.environ", {"ESPHOME_REMOTE_BUILD_PORT": "9999"}, clear=False):
        settings.parse_args(_ns(configuration=str(tmp_path)))
    assert settings.remote_build_port == 9999


def test_default_when_neither_cli_nor_env(tmp_path: Path) -> None:
    """Neither flag nor env → ``DEFAULT_REMOTE_BUILD_PORT``."""
    settings = DashboardSettings()
    # Empty string env entry covers the inherited-but-blank case.
    with patch.dict("os.environ", {"ESPHOME_REMOTE_BUILD_PORT": ""}, clear=False):
        settings.parse_args(_ns(configuration=str(tmp_path)))
    assert settings.remote_build_port == DEFAULT_REMOTE_BUILD_PORT


def test_invalid_env_falls_back_to_default(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-integer env value logs a warning and falls back to the default."""
    settings = DashboardSettings()
    with (
        patch.dict("os.environ", {"ESPHOME_REMOTE_BUILD_PORT": "not-a-port"}, clear=False),
        caplog.at_level("WARNING", logger="esphome_device_builder.controllers.config"),
    ):
        settings.parse_args(_ns(configuration=str(tmp_path)))
    assert settings.remote_build_port == DEFAULT_REMOTE_BUILD_PORT
    warnings = [r for r in caplog.records if "ESPHOME_REMOTE_BUILD_PORT" in r.getMessage()]
    assert warnings, "expected a warning about the malformed env var"
