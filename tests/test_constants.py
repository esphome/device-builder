"""Tests for shared constants helpers."""

from __future__ import annotations

from pathlib import Path

from esphome_device_builder.constants import SECRETS_FILENAME, is_secrets_file


def test_is_secrets_file_matches_by_basename() -> None:
    """is_secrets_file is True only for the secrets.yaml basename, str or Path."""
    assert is_secrets_file(SECRETS_FILENAME)
    assert is_secrets_file("secrets.yaml")
    assert is_secrets_file(Path("/config/esphome/secrets.yaml"))
    assert not is_secrets_file("kitchen.yaml")
    assert not is_secrets_file(Path("/config/secrets.yml"))
