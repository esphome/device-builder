"""Tests for the Native API encryption-key extraction + scanner flag.

Covers the helper layer (resolves through ESPHome's YAML loader so
``!secret`` / ``!include`` / packages all work) and the scan-time
``Device.api_encrypted`` flag that drives the dashboard's lock-icon
indicator.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from esphome_device_builder.helpers import device_yaml
from esphome_device_builder.helpers.device_yaml import (
    config_has_top_level_block,
    get_api_encryption_block,
    get_api_encryption_key,
    load_device_yaml,
)
from esphome_device_builder.models import Device

# ---------------------------------------------------------------------------
# Pure-helper paths — no disk
# ---------------------------------------------------------------------------


def test_get_api_encryption_block_returns_inner_dict() -> None:
    """An ``api: encryption: ...`` block is returned as a dict for the caller to inspect."""
    config = {"api": {"encryption": {"key": "abc=="}}}
    assert get_api_encryption_block(config) == {"key": "abc=="}


def test_get_api_encryption_block_none_when_no_api() -> None:
    assert get_api_encryption_block({"esphome": {"name": "x"}}) is None


def test_get_api_encryption_block_none_when_api_unencrypted() -> None:
    """Bare ``api:`` (Native API enabled but no encryption) → no block."""
    assert get_api_encryption_block({"api": {}}) is None


def test_get_api_encryption_block_handles_non_dict_inputs() -> None:
    """Bad config shapes (None, list, str) don't blow up the helper."""
    assert get_api_encryption_block(None) is None
    assert get_api_encryption_block({"api": "not-a-dict"}) is None
    assert get_api_encryption_block({"api": {"encryption": "not-a-dict"}}) is None


def test_get_api_encryption_key_returns_resolved_string() -> None:
    config = {"api": {"encryption": {"key": "ZGFzaA=="}}}
    assert get_api_encryption_key(config) == "ZGFzaA=="


def test_get_api_encryption_key_empty_when_missing() -> None:
    assert get_api_encryption_key({"api": {"encryption": {}}}) == ""
    assert get_api_encryption_key(None) == ""


def test_config_has_top_level_block() -> None:
    """``api`` / ``mqtt`` etc. are detected even with empty / null values."""
    assert config_has_top_level_block({"api": None}, "api") is True
    assert config_has_top_level_block({"mqtt": {"broker": "x"}}, "mqtt") is True
    assert config_has_top_level_block({"esphome": {}}, "api") is False
    assert config_has_top_level_block(None, "api") is False


# ---------------------------------------------------------------------------
# load_device_yaml — exercises ESPHome's loader, so this hits the file system
# ---------------------------------------------------------------------------


@pytest.fixture
def yaml_file(tmp_path: Path) -> Path:
    return tmp_path / "kitchen.yaml"


def test_load_device_yaml_parses_valid_config(yaml_file: Path) -> None:
    yaml_file.write_text(
        "esphome:\n"
        "  name: kitchen\n"
        "api:\n"
        '  encryption:\n    key: "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="\n'
    )
    config = load_device_yaml(yaml_file)
    assert config is not None
    assert get_api_encryption_key(config) == "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


def test_load_device_yaml_returns_none_on_parse_failure(yaml_file: Path) -> None:
    """An invalid draft mid-edit returns ``None`` instead of raising."""
    yaml_file.write_text("api: !\n  bad: [unterminated\n")
    assert load_device_yaml(yaml_file) is None


def test_load_device_yaml_resolves_secrets(tmp_path: Path) -> None:
    """``!secret`` references resolve through the sibling ``secrets.yaml``.

    The regex-on-raw-YAML approach the frontend used to do gave up
    here — backend resolution is the whole reason ``devices/get_api_key``
    exists.
    """
    (tmp_path / "secrets.yaml").write_text("api_key: 'AAAA=='\n")
    yaml_file = tmp_path / "kitchen.yaml"
    yaml_file.write_text(
        "esphome:\n  name: kitchen\napi:\n  encryption:\n    key: !secret api_key\n"
    )
    config = load_device_yaml(yaml_file)
    assert get_api_encryption_key(config) == "AAAA=="


# ---------------------------------------------------------------------------
# Scan-time integration — load_device_from_storage drives the Device flags
# the frontend reads to render the lock indicator.
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect ``ext_storage_path`` into ``tmp_path`` and bypass StorageJSON.

    ``load_device_from_storage`` walks ``CORE.config_path`` for the
    StorageJSON sidecar, which isn't set in unit tests. Point the helper
    at the temporary directory and force ``StorageJSON.load`` to return
    ``None`` so each test exercises the YAML + flag plumbing only.
    """
    monkeypatch.setattr(
        device_yaml,
        "ext_storage_path",
        lambda config: tmp_path / f"{config}.json",
    )
    monkeypatch.setattr(device_yaml.StorageJSON, "load", staticmethod(lambda _p: None))
    return tmp_path


def _scan(yaml_path: Path, content: str) -> Device:
    """Write *content* to *yaml_path* and run it through the scanner helper."""
    yaml_path.write_text(content)
    return device_yaml.load_device_from_storage(yaml_path)


def test_load_device_from_storage_sets_api_encrypted_from_resolved_yaml(
    isolated_storage: Path,
) -> None:
    """Scanner output's ``api_encrypted`` reflects the resolved config."""
    device = _scan(
        isolated_storage / "kitchen.yaml",
        'esphome:\n  name: kitchen\napi:\n  encryption:\n    key: "ZGFzaA=="\n',
    )
    assert device.api_enabled is True
    assert device.api_encrypted is True


def test_load_device_from_storage_api_disabled_for_mqtt_only(
    isolated_storage: Path,
) -> None:
    """A device with no ``api:`` block reports neither flag — drives the no-lock case."""
    device = _scan(
        isolated_storage / "sensor.yaml",
        "esphome:\n  name: sensor\nmqtt:\n  broker: 192.168.1.10\n",
    )
    assert device.api_enabled is False
    assert device.api_encrypted is False
    assert device.uses_mqtt is True


def test_load_device_from_storage_falls_back_for_invalid_draft(
    isolated_storage: Path,
) -> None:
    """Mid-edit drafts where ``yaml_util.load_yaml`` fails still get usable flags.

    The lock indicator would otherwise blink off the moment the user
    typed a syntax error. Raw-text fallback keeps the signal stable.
    """
    # Top-level ``api:`` with ``encryption:``, plus a deliberate syntax
    # error further down so ``yaml_util.load_yaml`` returns ``None`` and
    # we fall through to the raw-text heuristic.
    device = _scan(
        isolated_storage / "broken.yaml",
        "esphome:\n  name: broken\n"
        'api:\n  encryption:\n    key: "ZGFzaA=="\n'
        "sensor:\n  - platform: !\n    bad: [unterminated\n",
    )
    assert device.api_enabled is True
    assert device.api_encrypted is True
