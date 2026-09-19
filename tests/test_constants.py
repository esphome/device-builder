"""Tests for shared constants helpers."""

from __future__ import annotations

from pathlib import Path

import pytest
from esphome.const import SECRETS_FILES

from esphome_device_builder.constants import (
    SECRETS_FILENAME,
    SECRETS_FILENAMES,
    is_device_config_name,
    is_secrets_file,
)


def test_is_secrets_file_matches_by_basename() -> None:
    """is_secrets_file is True for either secrets file spelling, str or Path."""
    assert is_secrets_file(SECRETS_FILENAME)
    assert is_secrets_file("secrets.yaml")
    assert is_secrets_file(Path("/config/esphome/secrets.yaml"))
    assert is_secrets_file("SECRETS.YAML")
    assert is_secrets_file("\u017fecrets.yaml")
    assert is_secrets_file("secrets.yaml.")
    assert is_secrets_file("secrets.yaml ")
    assert not is_secrets_file("kitchen.yaml")
    assert is_secrets_file(Path("/config/secrets.yml"))
    assert not is_secrets_file("secrets.json")
    assert is_secrets_file("config\\secrets.yaml")


def test_secrets_filenames_match_esphome() -> None:
    assert set(SECRETS_FILENAMES) == set(SECRETS_FILES)


@pytest.mark.parametrize(
    "name",
    ["kitchen.yaml", "Kitchen.YML", "porch (1).yaml", "bedroom~1.yaml"],
)
def test_is_device_config_name_accepts_yaml_names(name: str) -> None:
    assert is_device_config_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "secrets.yaml",
        "SECRETS.YML",
        "secrets.yaml::$DATA",
        "SECRET~1.YAM",
        "notes.txt",
        "kitchen.yaml.",
        "kitchen.yaml ",
        "sub/kitchen.yaml",
        "sub\\kitchen.yaml",
        "../../etc/passwd.yaml",
        "kitchen\x00.yaml",
        "config\\secrets.yaml",
    ],
)
def test_is_device_config_name_refuses_secrets_aliases_and_other_files(name: str) -> None:
    assert not is_device_config_name(name)
