"""Tests for the shared in-process-then-subprocess config resolve."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from esphome_device_builder.controllers.devices import resolve as resolve_module
from esphome_device_builder.controllers.devices.resolve import (
    resolve_config,
    resolve_config_subprocess,
)
from esphome_device_builder.helpers.device_yaml import EsphomeConfigUnavailableError

from .conftest import ESPHOME_CONFIG_STUB_TARGET, MakeControllerFactory

PLAIN_YAML = "esphome:\n  name: kitchen\n\napi:\n"
INLINE_PACKAGE_YAML = "packages:\n  v:\n    api:\n      port: 6054\n\nesphome:\n  name: kitchen\n"
UNMERGEABLE_PACKAGE_YAML = "packages:\n  v: github://x/y.yaml\n\nesphome:\n  name: kitchen\n"
INCLUDED_API_YAML = "esphome:\n  name: kitchen\n\napi: !include api.yaml\n"
RESOLVED = {"esphome": {"name": "kitchen"}, "api": {}}


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
    """A load with nothing deferred is the answer; ``esphome config`` never spawns."""
    subprocess = AsyncMock()
    monkeypatch.setattr(ESPHOME_CONFIG_STUB_TARGET, subprocess)
    ctrl = make_controller(tmp_path, esphome_cmd=["esphome"])
    (tmp_path / "kitchen.yaml").write_text(yaml_text, encoding="utf-8")

    config = await resolve_config(ctrl, tmp_path / "kitchen.yaml")

    assert config is not None and "api" in config and "packages" not in config
    subprocess.assert_not_awaited()


@pytest.mark.parametrize(
    "yaml_text",
    [
        pytest.param(UNMERGEABLE_PACKAGE_YAML, id="unmerged_package"),
        pytest.param(INCLUDED_API_YAML, id="deferred_include"),
        pytest.param(": :", id="unparsable"),
    ],
)
async def test_resolve_config_falls_back_to_the_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
    yaml_text: str,
) -> None:
    """Deferred work the loader can't finish hands the whole resolve to ``esphome config``."""
    subprocess = AsyncMock(return_value=RESOLVED)
    monkeypatch.setattr(ESPHOME_CONFIG_STUB_TARGET, subprocess)
    ctrl = make_controller(tmp_path, esphome_cmd=["esphome"])
    (tmp_path / "kitchen.yaml").write_text(yaml_text, encoding="utf-8")
    (tmp_path / "api.yaml").write_text("encryption:\n  key: x\n", encoding="utf-8")

    assert await resolve_config(ctrl, tmp_path / "kitchen.yaml") == RESOLVED
    subprocess.assert_awaited_once()


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param("no_cli", id="no_cli"),
        pytest.param("unavailable", id="unavailable"),
        pytest.param("invalid", id="invalid"),
    ],
)
async def test_resolve_config_collapses_every_subprocess_failure_to_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
    mode: str,
) -> None:
    """No CLI, an infra fault, and an invalid config all read as unresolvable."""
    if mode == "unavailable":
        subprocess = AsyncMock(side_effect=EsphomeConfigUnavailableError("x"))
    else:
        subprocess = AsyncMock(return_value=None)
    monkeypatch.setattr(ESPHOME_CONFIG_STUB_TARGET, subprocess)
    ctrl = make_controller(tmp_path, esphome_cmd=[] if mode == "no_cli" else ["esphome"])
    (tmp_path / "kitchen.yaml").write_text(UNMERGEABLE_PACKAGE_YAML, encoding="utf-8")

    assert await resolve_config(ctrl, tmp_path / "kitchen.yaml") is None
    assert subprocess.await_count == (0 if mode == "no_cli" else 1)


async def test_resolve_config_accepts_a_path_the_caller_already_holds(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """A ``Path`` is used as is instead of being re-resolved from its name."""
    ctrl = make_controller(tmp_path, esphome_cmd=[])
    path = tmp_path / "kitchen.yaml"
    path.write_text(PLAIN_YAML, encoding="utf-8")

    assert await resolve_config(ctrl, path) == {"esphome": {"name": "kitchen"}, "api": None}


async def test_resolve_config_treats_a_stalled_in_process_load_as_deferred(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A loader stuck past the ceiling (a cold package clone) hands off to the subprocess."""
    subprocess = AsyncMock(return_value=RESOLVED)
    monkeypatch.setattr(ESPHOME_CONFIG_STUB_TARGET, subprocess)
    monkeypatch.setattr(resolve_module, "ESPHOME_CONFIG_TIMEOUT", 0.05)

    def stalled(settings, configuration):
        time.sleep(0.3)
        return tmp_path / "kitchen.yaml", {"esphome": {"name": "kitchen"}}

    monkeypatch.setattr(resolve_module, "_locate_and_load", stalled)
    ctrl = make_controller(tmp_path, esphome_cmd=["esphome"])
    (tmp_path / "kitchen.yaml").write_text(PLAIN_YAML, encoding="utf-8")

    assert await resolve_config(ctrl, tmp_path / "kitchen.yaml") == RESOLVED
    subprocess.assert_awaited_once()


async def test_resolve_config_subprocess_skips_the_executor_for_a_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """The subprocess-only entry takes a located ``Path`` and never touches the thread pool."""
    subprocess = AsyncMock(return_value=RESOLVED)
    monkeypatch.setattr(ESPHOME_CONFIG_STUB_TARGET, subprocess)
    monkeypatch.setattr(resolve_module, "run_in_executor", AsyncMock(side_effect=AssertionError))
    ctrl = make_controller(tmp_path, esphome_cmd=["esphome"])

    assert await resolve_config_subprocess(ctrl, tmp_path / "kitchen.yaml") == RESOLVED
    subprocess.assert_awaited_once()
