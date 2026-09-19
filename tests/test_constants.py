"""Tests for shared constants helpers."""

from __future__ import annotations

from pathlib import Path

from esphome.const import SECRETS_FILES

from esphome_device_builder.constants import SECRETS_FILENAME, SECRETS_FILENAMES, is_secrets_file


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


def test_secrets_filenames_match_esphome() -> None:
    assert set(SECRETS_FILENAMES) == set(SECRETS_FILES)
