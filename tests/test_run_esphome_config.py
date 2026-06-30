"""Tests for ``helpers.device_yaml.run_esphome_config``.

The subprocess primitive behind ``/json-config`` and the
``devices/get_api_key`` package fallback — it runs ``esphome config
--show-secrets`` and parses the fully-resolved output.
"""

from __future__ import annotations

import asyncio
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
    """Exit 0 returns a parsed dict; ``!lambda`` survives as a string, not a raise."""
    stdout = b"esphome:\n  name: kitchen\nlambda_value: !lambda 'return 1;'\n"
    _patch_spawn(monkeypatch, _fake_proc(stdout, b"", 0))

    config = await run_esphome_config(["esphome"], Path("kitchen.yaml"))

    assert config is not None
    assert config["esphome"]["name"] == "kitchen"
    # Unknown tag rendered as a string → JSON-native, serialises cleanly.
    assert isinstance(config["lambda_value"], str)
    assert dumps_str(config)


async def test_non_scalar_unknown_tag_falls_back_to_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-scalar unknown tag yields the bare tag, not a node-pair-list repr."""
    stdout = b"top: !weird\n  a: 1\n  b: 2\n"
    _patch_spawn(monkeypatch, _fake_proc(stdout, b"", 0))

    config = await run_esphome_config(["esphome"], Path("kitchen.yaml"))

    assert config == {"top": "!weird"}
    assert dumps_str(config)


async def test_nonzero_exit_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A validation failure (rc!=0) yields ``None``."""
    _patch_spawn(monkeypatch, _fake_proc(b"Failed config\n", b"boom", 2))

    assert await run_esphome_config(["esphome"], Path("kitchen.yaml")) is None


async def test_unparsable_output_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exit 0 but malformed stdout degrades to ``None`` rather than raising."""
    _patch_spawn(monkeypatch, _fake_proc(b"not yaml: [unterminated", b"", 0))

    assert await run_esphome_config(["esphome"], Path("kitchen.yaml")) is None


async def test_non_mapping_output_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Output that parses to a list/scalar (not a mapping) returns ``None``."""
    _patch_spawn(monkeypatch, _fake_proc(b"- one\n- two\n", b"", 0))

    assert await run_esphome_config(["esphome"], Path("kitchen.yaml")) is None


async def test_spawn_oserror_raises_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A spawn failure (binary missing / FD exhaustion) is a retryable infra fault."""

    async def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("simulated spawn failure")

    monkeypatch.setattr(resolve_mod, "create_subprocess_exec", _boom)

    with pytest.raises(resolve_mod.EsphomeConfigUnavailableError):
        await run_esphome_config(["esphome"], Path("kitchen.yaml"))


async def test_signal_kill_raises_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A negative return code (killed by a signal) is infra, not invalid config."""
    _patch_spawn(monkeypatch, _fake_proc(b"", b"", -9))

    with pytest.raises(resolve_mod.EsphomeConfigUnavailableError):
        await run_esphome_config(["esphome"], Path("kitchen.yaml"))


async def test_timeout_kills_and_raises_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wedged subprocess past the timeout is killed and surfaces as infra fault."""
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

    with pytest.raises(resolve_mod.EsphomeConfigUnavailableError):
        await run_esphome_config(["esphome"], Path("kitchen.yaml"))
    assert killed == [True]


async def test_cancellation_kills_proc_and_reraises(monkeypatch: pytest.MonkeyPatch) -> None:
    """External cancellation during the wait kills the proc and propagates."""
    proc = MagicMock()
    proc.communicate = AsyncMock(side_effect=asyncio.CancelledError)
    killed: list[bool] = []
    _patch_spawn(monkeypatch, proc)
    monkeypatch.setattr(resolve_mod, "kill_quietly", lambda _p: killed.append(True))

    with pytest.raises(asyncio.CancelledError):
        await run_esphome_config(["esphome"], Path("kitchen.yaml"))
    assert killed == [True]


async def test_caps_concurrent_subprocesses(monkeypatch: pytest.MonkeyPatch) -> None:
    """No more than ``_MAX_CONCURRENT_CONFIG`` subprocesses run at once."""
    # A contended ``asyncio.Semaphore`` binds to its loop; give this test its
    # own gate so it doesn't bind the shared module-level one to a test loop.
    monkeypatch.setattr(
        resolve_mod, "_config_semaphore", asyncio.Semaphore(resolve_mod._MAX_CONCURRENT_CONFIG)
    )
    gate = asyncio.Event()
    active = 0
    peak = 0

    async def _communicate() -> tuple[bytes, bytes]:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await gate.wait()
        active -= 1
        return b"esphome:\n  name: x\n", b""

    async def _spawn(*_args: Any, **_kwargs: Any) -> Any:
        proc = MagicMock()
        proc.communicate = _communicate
        proc.returncode = 0
        return proc

    monkeypatch.setattr(resolve_mod, "create_subprocess_exec", _spawn)
    cap = resolve_mod._MAX_CONCURRENT_CONFIG
    tasks = [
        asyncio.create_task(run_esphome_config(["esphome"], Path("x.yaml"))) for _ in range(cap + 3)
    ]
    # Let the first batch acquire the gate and pile up against the cap.
    for _ in range(100):
        if peak >= cap:
            break
        await asyncio.sleep(0)

    assert peak == cap  # the cap was reached...
    gate.set()
    results = await asyncio.gather(*tasks)

    assert peak == cap  # ...and never exceeded, even as the rest drained
    assert all(r == {"esphome": {"name": "x"}} for r in results)
