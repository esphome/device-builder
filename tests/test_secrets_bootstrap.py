"""Tests for the ``secrets.yaml`` bootstrap in ``DashboardSettings.parse_args``.

The bootstrap creates an empty ``secrets.yaml`` on first startup so the
Secrets editor opens a real file and ``!secret`` references have a target.
No Wi-Fi placeholders are seeded: credentials are collected per-device in
the create wizard (which writes them here), and generation is adaptive, so
a device created before any Wi-Fi secret exists gets a no-network stub.
"""

from __future__ import annotations

import os
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from esphome_device_builder.controllers.config import DashboardSettings
from esphome_device_builder.helpers.secrets_state import (
    PLACEHOLDER_WIFI_PASSWORD,
    PLACEHOLDER_WIFI_SSID,
    read_secrets_yaml,
)


def _ns(**overrides: object) -> Namespace:
    """Minimal argparse namespace ``parse_args`` reads.

    Mirrors the helper in ``test_credentials_env.py``; copied here
    rather than imported to keep the bootstrap test self-contained.
    """
    base: dict[str, object] = {
        "configuration": "",
        "username": "",
        "password": "",
        "log_level": "info",
        "port": 6052,
        "host": "0.0.0.0",
        "ingress_port": 8099,
        "ingress_host": "",
        "trusted_domains": "",
        "dev": False,
        "dashboard_path": "",
        "yaml_quote_style": "double",
        "executor_workers": 0,
    }
    base.update(overrides)
    return Namespace(**base)


def _bootstrap(tmp_path: Path) -> DashboardSettings:
    settings = DashboardSettings()
    with patch.dict(os.environ, {}, clear=True):
        settings.parse_args(_ns(configuration=str(tmp_path)))
    return settings


def test_bootstrap_creates_secrets_without_wifi_placeholders(
    tmp_path: Path,
) -> None:
    _bootstrap(tmp_path)
    secrets_path = tmp_path / "secrets.yaml"
    assert secrets_path.exists()
    content = secrets_path.read_text()
    # No Wi-Fi keys are seeded — the wizard collects and writes them, and
    # a fresh-install create emits a no-network stub until then.
    assert "wifi_ssid" not in content
    assert "wifi_password" not in content


def test_bootstrap_does_not_overwrite_existing_secrets(tmp_path: Path) -> None:
    """An existing ``secrets.yaml`` with real values is left alone."""
    existing = "wifi_ssid: home_network\nwifi_password: real_password\n"
    (tmp_path / "secrets.yaml").write_text(existing)
    _bootstrap(tmp_path)
    assert (tmp_path / "secrets.yaml").read_text() == existing


def test_bootstrap_migrates_away_seeded_wifi_placeholders(tmp_path: Path) -> None:
    """An existing install's leftover placeholder Wi-Fi secrets are stripped."""
    (tmp_path / "secrets.yaml").write_text(
        f'api_key: keep\nwifi_ssid: "{PLACEHOLDER_WIFI_SSID}"\n'
        f'wifi_password: "{PLACEHOLDER_WIFI_PASSWORD}"\n'
    )
    _bootstrap(tmp_path)
    assert read_secrets_yaml(tmp_path) == {"api_key": "keep"}
