"""Tests for ``helpers.device_yaml.run_esphome_config``.

The subprocess primitive behind ``/json-config`` and the
``devices/get_api_key`` package fallback — it runs ``esphome config
--show-secrets`` and parses the fully-resolved output.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import esphome_device_builder.helpers.device_yaml._resolve as resolve_mod
from esphome_device_builder.helpers.device_yaml import run_esphome_config
from esphome_device_builder.helpers.json import dumps_str


def _fake_proc(stdout: bytes, stderr: bytes, returncode: int) -> MagicMock:
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    return proc


def _patch_spawn(mp: pytest.MonkeyPatch, proc: MagicMock) -> None:
    async def _spawn(*_args: Any, **_kwargs: Any) -> Any:
        return proc

    mp.setattr(resolve_mod, "create_subprocess_exec", _spawn)


async def test_parses_resolved_output_and_stringifies_unknown_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rc 0 → parsed dict; ``!lambda`` survives as a plain string, not a raise."""
    stdout = b"esphome:\n  name: kitchen\nlambda_value: !lambda 'return 1;'\n"
    _patch_spawn(monkeypatch, _fake_proc(stdout, b"", 0))

    rc, config, _stderr = await run_esphome_config(["esphome"], Path("kitchen.yaml"))

    assert rc == 0
    assert config is not None
    assert config["esphome"]["name"] == "kitchen"
    # Unknown tag rendered as a string → JSON-native, serialises cleanly.
    assert isinstance(config["lambda_value"], str)
    assert dumps_str(config)


async def test_nonzero_exit_returns_none_config_with_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A validation failure (rc!=0) yields ``(rc, None, stderr)``."""
    _patch_spawn(monkeypatch, _fake_proc(b"Failed config\n", b"boom", 2))

    rc, config, stderr = await run_esphome_config(["esphome"], Path("kitchen.yaml"))

    assert rc == 2
    assert config is None
    assert stderr == "boom"


async def test_unparsable_output_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rc 0 but malformed stdout degrades to ``None`` rather than raising."""
    _patch_spawn(monkeypatch, _fake_proc(b"not yaml: [unterminated", b"", 0))

    _rc, config, _stderr = await run_esphome_config(["esphome"], Path("kitchen.yaml"))

    assert config is None


async def test_non_mapping_output_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Output that parses to a list/scalar (not a mapping) returns ``None``."""
    _patch_spawn(monkeypatch, _fake_proc(b"- one\n- two\n", b"", 0))

    _rc, config, _stderr = await run_esphome_config(["esphome"], Path("kitchen.yaml"))

    assert config is None


async def test_spawn_oserror_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A spawn failure (binary missing / FD exhaustion) degrades to ``None``."""

    async def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("simulated spawn failure")

    monkeypatch.setattr(resolve_mod, "create_subprocess_exec", _boom)

    rc, config, _stderr = await run_esphome_config(["esphome"], Path("kitchen.yaml"))

    assert rc is None
    assert config is None


async def test_timeout_kills_and_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wedged subprocess past the timeout is killed and returns ``None``."""
    proc = MagicMock()
    proc.communicate = AsyncMock(side_effect=TimeoutError)
    proc.wait = AsyncMock()
    proc.returncode = None
    killed: list[bool] = []
    _patch_spawn(monkeypatch, proc)
    monkeypatch.setattr(resolve_mod, "kill_quietly", lambda _p: killed.append(True))

    async def _instant_timeout(coro: Any, timeout: float) -> Any:
        coro.close()
        raise TimeoutError

    monkeypatch.setattr(resolve_mod.asyncio, "wait_for", _instant_timeout)

    _rc, config, _stderr = await run_esphome_config(["esphome"], Path("kitchen.yaml"))

    assert config is None
    assert killed == [True]
