"""
Unit tests for :mod:`helpers.config_bundle`.

The bundle helper wraps :class:`esphome.bundle.ConfigBundleCreator`
+ :func:`esphome.config.read_config`, both of which depend on
the global :data:`esphome.core.CORE`. The tests monkeypatch the
two upstream functions so the helper's plumbing
(executor hop, lock, CORE save/restore, error mapping) is
exercised without running real ESPHome validation against a
test YAML.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from esphome_device_builder.helpers import config_bundle
from esphome_device_builder.helpers.config_bundle import build_yaml_bundle


@pytest.mark.asyncio
async def test_build_yaml_bundle_returns_create_bundle_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: ``read_config`` returns a config and ``ConfigBundleCreator`` packs it."""
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n", encoding="utf-8")

    fake_config: dict[str, Any] = {"esphome": {"name": "kitchen"}}
    expected_bytes = b"GZIPPED-TAR-BYTES"

    def _fake_read_config(_subs: dict[str, Any]) -> dict[str, Any]:
        return fake_config

    class _FakeBundleResult:
        data = expected_bytes

    class _FakeCreator:
        def __init__(self, _config: dict[str, Any]) -> None:
            pass

        def create_bundle(self) -> _FakeBundleResult:
            return _FakeBundleResult()

    monkeypatch.setattr(config_bundle, "read_config", _fake_read_config)
    monkeypatch.setattr(config_bundle, "ConfigBundleCreator", _FakeCreator)

    result = await build_yaml_bundle(yaml_path)
    assert result == expected_bytes


@pytest.mark.asyncio
async def test_build_yaml_bundle_missing_yaml_raises_file_not_found(
    tmp_path: Path,
) -> None:
    """A missing YAML at *yaml_path* raises :class:`FileNotFoundError` upfront.

    The helper checks ``yaml_path.is_file()`` before swapping
    CORE state — so a typo in the WS command's ``configuration``
    arg never lands inside the lock or the executor and never
    leaves CORE state ambiguous on failure.
    """
    with pytest.raises(FileNotFoundError):
        await build_yaml_bundle(tmp_path / "missing.yaml")


@pytest.mark.asyncio
async def test_build_yaml_bundle_restores_core_state_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CORE.config_path / CORE.config are restored after a successful build."""
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    saved_config_path = config_bundle.CORE.config_path
    saved_config = config_bundle.CORE.config

    def _fake_read_config(_subs: dict[str, Any]) -> dict[str, Any]:
        return {"esphome": {"name": "kitchen"}}

    class _FakeCreator:
        def __init__(self, _config: dict[str, Any]) -> None:
            pass

        def create_bundle(self) -> Any:
            class _R:
                data = b"x"

            return _R()

    monkeypatch.setattr(config_bundle, "read_config", _fake_read_config)
    monkeypatch.setattr(config_bundle, "ConfigBundleCreator", _FakeCreator)

    await build_yaml_bundle(yaml_path)
    assert config_bundle.CORE.config_path == saved_config_path
    assert config_bundle.CORE.config == saved_config


@pytest.mark.asyncio
async def test_build_yaml_bundle_raises_when_read_config_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``read_config`` returning ``None`` (its sentinel for failure) raises RuntimeError.

    Upstream returns ``None`` instead of raising on certain
    validation failures (the legacy CLI's ``return 2`` exit
    code path); the helper translates that to a structured
    raise so the caller doesn't have to know about the
    sentinel.
    """
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n", encoding="utf-8")

    def _fake_read_config(_subs: Any) -> Any:
        return None

    monkeypatch.setattr(config_bundle, "read_config", _fake_read_config)

    with pytest.raises(RuntimeError, match="returned None"):
        await build_yaml_bundle(yaml_path)


@pytest.mark.asyncio
async def test_build_yaml_bundle_restores_core_state_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CORE state is restored even when ``read_config`` raises."""
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    saved_config_path = config_bundle.CORE.config_path
    saved_config = config_bundle.CORE.config

    def _fake_read_config(_subs: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("schema-broken")

    monkeypatch.setattr(config_bundle, "read_config", _fake_read_config)

    with pytest.raises(RuntimeError):
        await build_yaml_bundle(yaml_path)
    assert config_bundle.CORE.config_path == saved_config_path
    assert config_bundle.CORE.config == saved_config
