"""End-to-end coverage for ``_schedule_storage_regenerate``.

Most callers of the regenerate path mock it as a ``MagicMock``
(see ``test_get_update_config.py`` and ``test_archive.py``) so the
fire-and-forget spawn doesn't run. That leaves the body of
``_schedule_storage_regenerate`` itself uncovered: the
duplicate-schedule guard, the failed-marker guard, the
``create_subprocess_exec`` call, the non-zero-exit handling, the
spawn-failure handling, and the post-success
``_persist_expected_config_hash`` + ``_scanner.reload`` chain.

These tests drive through the public ``update_config`` API but
let ``_schedule_storage_regenerate`` execute for real, with a
patched ``create_subprocess_exec`` returning a configurable
``FakeProc``. Background-task settling uses the same
``create_background_task`` plumbing the controller uses, so the
tests exercise the actual coroutine the production code spawns.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from esphome_device_builder.controllers.devices import DevicesController

from .conftest import MakeControllerFactory


class _FakeProc:
    """Minimal ``asyncio.subprocess.Process`` stand-in.

    ``communicate`` returns the configured stderr bytes;
    ``returncode`` carries the configured exit code. Only the
    bits ``_schedule_storage_regenerate`` reads.
    """

    def __init__(self, returncode: int = 0, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return (b"", self._stderr)


async def _drain(controller: DevicesController) -> None:
    """Wait for every background task ``_schedule_storage_regenerate`` queued.

    Drops ``return_exceptions=True`` so an unexpected crash inside the
    regenerate coroutine fails the test instead of silently masking
    as ``None`` in the gather result. Production swallows the error
    branches in its own ``try/except`` (and asserts on
    ``_regenerate_failed``); anything reaching the gather here is a
    bug we want surfaced.
    """
    pending: list[asyncio.Task] = controller._spawned_tasks  # type: ignore[attr-defined]
    if pending:
        await asyncio.gather(*pending)
        pending.clear()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regenerate_spawns_esphome_compile_only_generate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """Successful spawn → expected-hash persist + scanner reload.

    Pin the full success chain end-to-end. After
    ``update_config`` lands the YAML and queues the regenerate,
    the spawn returns 0; the controller persists
    ``expected_config_hash`` from ``build_info.json`` and reloads
    the scanner so the device's metadata refreshes without
    waiting for a real compile.
    """
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])
    captured_cmd: list[list[str]] = []

    async def _fake_spawn(*args: str, **_kwargs: Any) -> _FakeProc:
        captured_cmd.append(list(args))
        return _FakeProc(returncode=0)

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.controller.create_subprocess_exec",
        _fake_spawn,
    )
    persist_calls: list[str] = []

    async def _fake_persist(_self: Any, configuration: str) -> None:
        persist_calls.append(configuration)

    monkeypatch.setattr(DevicesController, "_persist_expected_config_hash", _fake_persist)

    await controller.update_config(
        configuration="kitchen.yaml", content="esphome:\n  name: kitchen\n"
    )
    await _drain(controller)

    # esphome --dashboard compile --only-generate <config_path>
    assert captured_cmd == [
        [
            "esphome",
            "--dashboard",
            "compile",
            "--only-generate",
            str(tmp_path / "kitchen.yaml"),
        ]
    ]
    assert persist_calls == ["kitchen.yaml"]
    reload_calls = [c for c in controller._scanner.calls if c[0] == "reload"]
    assert reload_calls == [("reload", "kitchen.yaml")]
    # Pending guard cleared in the ``finally``.
    assert controller._regenerate_pending == set()
    # Success → not in failed set.
    assert controller._regenerate_failed == set()


# ---------------------------------------------------------------------------
# Early-return guards
# ---------------------------------------------------------------------------


def test_regenerate_skips_when_esphome_cmd_unset(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """``_esphome_cmd`` empty (``start()`` hasn't run) → no-op.

    Synchronous test — the guard is the very first check in the
    function and short-circuits before scheduling the
    background task. No spawn, no _regenerate_pending mutation.
    """
    # ``esphome_cmd=[]`` triggers the early-return guard.
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=[])

    controller._schedule_storage_regenerate("kitchen.yaml")

    assert controller._spawned_tasks == []  # type: ignore[attr-defined]
    assert controller._regenerate_pending == set()


def test_regenerate_skips_duplicate_schedule(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Configuration already in ``_regenerate_pending`` → second schedule is a no-op.

    Without this, repeated saves while a regenerate is already
    in flight would queue N background tasks all racing on the
    same YAML.
    """
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])
    controller._regenerate_pending.add("kitchen.yaml")

    controller._schedule_storage_regenerate("kitchen.yaml")

    assert controller._spawned_tasks == []  # type: ignore[attr-defined]


def test_regenerate_skips_after_failed_marker(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Configuration in ``_regenerate_failed`` → no respin.

    The marker is cleared by ``_on_scan_change`` when the YAML's
    cache key changes (i.e. the user actually edited it). Until
    then a respin would just burn another subprocess on the same
    bad input.
    """
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])
    controller._regenerate_failed.add("kitchen.yaml")

    controller._schedule_storage_regenerate("kitchen.yaml")

    assert controller._spawned_tasks == []  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regenerate_marks_failed_on_nonzero_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """``esphome compile`` exiting non-zero → no reload, ``_regenerate_failed`` set.

    Captures the typical "user saved a YAML with a syntax error"
    case. The controller has to remember the failure so the
    next save (with the same broken YAML) doesn't re-spawn.
    """
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])

    async def _fake_spawn(*_args: str, **_kwargs: Any) -> _FakeProc:
        return _FakeProc(returncode=1, stderr=b"YAML parse error at line 3")

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.controller.create_subprocess_exec",
        _fake_spawn,
    )
    persist_calls: list[str] = []

    async def _fake_persist(_self: Any, configuration: str) -> None:
        persist_calls.append(configuration)

    monkeypatch.setattr(DevicesController, "_persist_expected_config_hash", _fake_persist)

    await controller.update_config(configuration="kitchen.yaml", content="not: valid: yaml\n")
    await _drain(controller)

    # Failure → reload skipped, persist skipped, failed marker set.
    assert not any(c[0] == "reload" for c in controller._scanner.calls)
    assert persist_calls == []
    assert controller._regenerate_failed == {"kitchen.yaml"}
    # Pending cleared via the ``finally``.
    assert controller._regenerate_pending == set()


@pytest.mark.asyncio
async def test_regenerate_marks_failed_on_spawn_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """``create_subprocess_exec`` raising → ``_regenerate_failed`` set.

    Triggers when ``esphome`` is missing from PATH (broken pip
    install, dashboard running outside its venv). The pending
    marker has to clear via the outer ``finally`` so a follow-up
    schedule on the same configuration isn't blocked by the
    duplicate-schedule guard.
    """
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])

    async def _broken_spawn(*_args: str, **_kwargs: Any) -> _FakeProc:
        raise OSError("esphome: command not found")

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.controller.create_subprocess_exec",
        _broken_spawn,
    )
    monkeypatch.setattr(
        DevicesController,
        "_persist_expected_config_hash",
        AsyncMock(),
    )

    await controller.update_config(
        configuration="kitchen.yaml", content="esphome:\n  name: kitchen\n"
    )
    await _drain(controller)

    assert not any(c[0] == "reload" for c in controller._scanner.calls)
    assert controller._regenerate_failed == {"kitchen.yaml"}
    assert controller._regenerate_pending == set()


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regenerate_pending_blocks_in_flight_dupe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A second schedule mid-spawn doesn't queue a duplicate task.

    Pin the runtime contract of the duplicate-schedule guard
    *while a spawn is in flight* (the static test above only
    pre-sets the flag). Drive a real spawn that blocks on a
    sentinel event; while it's blocked, schedule again — the
    guard fires and no second task lands.
    """
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])
    in_flight = asyncio.Event()
    release = asyncio.Event()

    async def _hold(*_args: str, **_kwargs: Any) -> _FakeProc:
        in_flight.set()
        await release.wait()
        return _FakeProc(returncode=0)

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.controller.create_subprocess_exec",
        _hold,
    )
    monkeypatch.setattr(
        DevicesController,
        "_persist_expected_config_hash",
        AsyncMock(),
    )

    controller._schedule_storage_regenerate("kitchen.yaml")
    await asyncio.wait_for(in_flight.wait(), timeout=2.0)
    assert controller._regenerate_pending == {"kitchen.yaml"}

    # Second schedule while the first is still inside ``communicate``.
    controller._schedule_storage_regenerate("kitchen.yaml")
    # Only the original task exists.
    assert len(controller._spawned_tasks) == 1  # type: ignore[attr-defined]

    release.set()
    await _drain(controller)
    assert controller._regenerate_pending == set()
