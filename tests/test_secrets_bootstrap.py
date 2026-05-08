"""Tests for the ``secrets.yaml`` bootstrap in ``DashboardSettings.parse_args``.

The bootstrap creates a placeholder ``secrets.yaml`` on first
startup so ``!secret wifi_ssid`` / ``!secret wifi_password``
references in generated YAML resolve cleanly. The placeholders
must be **non-empty** — ESPHome's ``wifi`` validator rejects an
empty SSID with "SSID can't be empty.", which would surface to
the user as "Failed to create device: SSID can't be empty." on
the very first wizard run.
"""

from __future__ import annotations

import os
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from esphome_device_builder.controllers.config import DashboardSettings


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


def test_bootstrap_creates_secrets_with_non_empty_placeholders(
    tmp_path: Path,
) -> None:
    _bootstrap(tmp_path)
    content = (tmp_path / "secrets.yaml").read_text()
    # The placeholders must be non-empty so the YAML round-trips
    # through ESPHome's wifi validator without "SSID can't be empty.".
    assert 'wifi_ssid: ""' not in content
    assert 'wifi_password: ""' not in content
    assert "wifi_ssid:" in content
    assert "wifi_password:" in content
    # The placeholder text should be obvious enough that a user
    # who skipped the explanation comment still recognises the
    # value as something to replace.
    assert "REPLACE" in content


def test_bootstrap_does_not_overwrite_existing_secrets(tmp_path: Path) -> None:
    """An existing ``secrets.yaml`` is left alone — no clobbering user data."""
    existing = "wifi_ssid: home_network\nwifi_password: real_password\n"
    (tmp_path / "secrets.yaml").write_text(existing)
    _bootstrap(tmp_path)
    assert (tmp_path / "secrets.yaml").read_text() == existing
