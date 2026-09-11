"""Tests for the shared in-process-then-subprocess config resolve."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from esphome_device_builder.controllers.devices.resolve import resolve_config
from esphome_device_builder.helpers.device_yaml import EsphomeConfigUnavailableError

from .conftest import MakeControllerFactory

PLAIN_YAML = "esphome:\n  name: kitchen\n\napi:\n"
UNMERGEABLE_PACKAGE_YAML = "packages:\n  v: github://x/y.yaml\n\nesphome:\n  name: kitchen\n"
INLINE_PACKAGE_YAML = "packages:\n  v:\n    api:\n      port: 6054\n\nesphome:\n  name: kitchen\n"


def _patch_subprocess(monkeypatch: pytest.MonkeyPatch, mock: AsyncMock) -> None:
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.resolve.run_esphome_config", mock
    )


@pytest.mark.parametrize(
    "yaml_text",
    [pytest.param(PLAIN_YAML, id="plain"), pytest.param(INLINE_PACKAGE_YAML, id="merged")],
)
async def test_resolve_config_in_process_result_skips_the_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
    yaml_text: str,
) -> None:
    subprocess = AsyncMock()
    _patch_subprocess(monkeypatch, subprocess)
    ctrl = make_controller(tmp_path, esphome_cmd=["esphome"])
    (tmp_path / "kitchen.yaml").write_text(yaml_text, encoding="utf-8")

    config = await resolve_config(ctrl, "kitchen.yaml")

    assert config is not None and "api" in config and "packages" not in config
    subprocess.assert_not_awaited()


@pytest.mark.parametrize(
    "yaml_text",
    [
        pytest.param(UNMERGEABLE_PACKAGE_YAML, id="unmerged_package"),
        pytest.param(": :", id="unparsable"),
    ],
)
async def test_resolve_config_falls_back_to_the_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
    yaml_text: str,
) -> None:
    subprocess = AsyncMock(return_value={"esphome": {"name": "kitchen"}, "api": {}})
    _patch_subprocess(monkeypatch, subprocess)
    ctrl = make_controller(tmp_path, esphome_cmd=["esphome"])
    (tmp_path / "kitchen.yaml").write_text(yaml_text, encoding="utf-8")

    config = await resolve_config(ctrl, "kitchen.yaml")

    assert config == {"esphome": {"name": "kitchen"}, "api": {}}
    subprocess.assert_awaited_once()


@pytest.mark.parametrize(
    ("esphome_cmd", "subprocess"),
    [
        pytest.param([], AsyncMock(), id="no_cli"),
        pytest.param(
            ["esphome"], AsyncMock(side_effect=EsphomeConfigUnavailableError("x")), id="unavailable"
        ),
        pytest.param(["esphome"], AsyncMock(return_value=None), id="invalid"),
    ],
)
async def test_resolve_config_collapses_every_subprocess_failure_to_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
    esphome_cmd: list[str],
    subprocess: AsyncMock,
) -> None:
    _patch_subprocess(monkeypatch, subprocess)
    ctrl = make_controller(tmp_path, esphome_cmd=esphome_cmd)
    (tmp_path / "kitchen.yaml").write_text(UNMERGEABLE_PACKAGE_YAML, encoding="utf-8")

    assert await resolve_config(ctrl, "kitchen.yaml") is None
    if not esphome_cmd:
        subprocess.assert_not_awaited()
