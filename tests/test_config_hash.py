"""Tests for ``helpers/config_hash.compute_yaml_config_hash``.

Computing the hash needs the real ESPHome ``read_config()`` toolchain
on the path, so these run an end-to-end subprocess against a minimal
valid YAML in ``tmp_path``. They double as a smoke test that our
inline subprocess script imports cleanly under the dashboard's
interpreter.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from esphome_device_builder.helpers.config_hash import compute_yaml_config_hash

_MINIMAL_VALID_YAML = """
esphome:
  name: kitchen
esp32:
  variant: esp32c3
  framework:
    type: esp-idf
"""


@pytest.mark.asyncio
async def test_compute_returns_eight_char_hex_for_valid_config(tmp_path: Path) -> None:
    """A valid config produces a deterministic 8-char lowercase hex hash."""
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text(_MINIMAL_VALID_YAML)

    result = await compute_yaml_config_hash(yaml_path)

    assert result is not None
    assert len(result) == 8
    assert all(c in "0123456789abcdef" for c in result)


@pytest.mark.asyncio
async def test_compute_is_deterministic(tmp_path: Path) -> None:
    """Same YAML → same hash on subsequent runs (depends on FNV-1a determinism)."""
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text(_MINIMAL_VALID_YAML)

    first = await compute_yaml_config_hash(yaml_path)
    second = await compute_yaml_config_hash(yaml_path)

    assert first is not None
    assert first == second


@pytest.mark.asyncio
async def test_compute_hash_changes_with_content(tmp_path: Path) -> None:
    """Edits that change the resolved config produce a different hash."""
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text(_MINIMAL_VALID_YAML)
    before = await compute_yaml_config_hash(yaml_path)

    # Tweak a meaningful field — name change cascades through the
    # resolved config and so changes the hash.
    yaml_path.write_text(_MINIMAL_VALID_YAML.replace("name: kitchen", "name: bedroom"))
    after = await compute_yaml_config_hash(yaml_path)

    assert before is not None
    assert after is not None
    assert before != after


@pytest.mark.asyncio
async def test_compute_returns_none_for_invalid_yaml(tmp_path: Path) -> None:
    """Subprocess exits non-zero on validation failure → None (fall back to mtime)."""
    yaml_path = tmp_path / "broken.yaml"
    yaml_path.write_text("esphome:\n  name: !!! not valid\n")

    result = await compute_yaml_config_hash(yaml_path)

    assert result is None


@pytest.mark.asyncio
async def test_compute_returns_none_for_missing_file(tmp_path: Path) -> None:
    """Nonexistent path → None, no exception bubbled to the caller."""
    result = await compute_yaml_config_hash(tmp_path / "ghost.yaml")
    assert result is None
