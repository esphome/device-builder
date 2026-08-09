"""End-to-end coverage for ``_schedule_storage_regenerate``.

Most callers of the regenerate path mock it as a ``MagicMock``
(see ``test_get_update_config.py`` and ``test_archive.py``) so the
fire-and-forget spawn doesn't run. That leaves the body of
``_schedule_storage_regenerate`` itself uncovered: the
duplicate-schedule guard, the persisted-stamp consult (attempt
budget, backoff min-age, TTL), the ``create_subprocess_exec``
call, the failure stamping and retry arming, and the post-success
``_persist_expected_config_hash`` + ``_scanner.reload`` chain.

These tests let ``_schedule_storage_regenerate`` execute for real,
with a patched ``create_subprocess_exec`` returning a configurable
``FakeProc``. Background-task settling uses the same
``create_background_task`` plumbing the controller uses, so the
tests exercise the actual coroutine the production code spawns.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from esphome_device_builder.controllers._device_scanner import ScanChange
from esphome_device_builder.controllers.devices import DevicesController, storage_regen
from esphome_device_builder.controllers.devices._state import RegenState
from tests._storage_fixtures import write_storage_json
from tests.conftest import make_device, wait_until

from .conftest import MakeControllerFactory, MakeDbFactory


def _seed_store(controller: DevicesController, filename: str, **fields: Any) -> None:
    """Seed live-state fields into the in-RAM store (no save).

    Sync alternative to ``controller._metadata_store.update(...)``
    that doesn't try to schedule a debounced save — useful for
    test setup that runs before the asyncio loop kicks off the
    code under test.
    """
    existing = controller._metadata_store._state.get(filename, {})
    controller._metadata_store._state[filename] = {
        **existing,
        **{k: v for k, v in fields.items() if v is not None},
    }


def _read_store(controller: DevicesController, filename: str) -> dict[str, Any]:
    """Read the per-device store entry (caller asserts on individual keys)."""
    return controller._metadata_store.get(filename)


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


def _seed_attempts(
    controller: DevicesController, configuration: str, attempts: int, *, age: float
) -> None:
    """Seed a failure stamp for the YAML's current mtime, *age* seconds old."""
    mtime = (controller._db.settings.config_dir / configuration).stat().st_mtime
    _seed_store(
        controller,
        configuration,
        regen_failed_mtime=mtime,
        regen_failed_at=time.time() - age,
        regen_failed_attempts=attempts,
    )


async def _drain(controller: DevicesController) -> None:
    """Wait for every background task ``_schedule_storage_regenerate`` queued.

    Drops ``return_exceptions=True`` so an unexpected crash inside the
    regenerate coroutine fails the test instead of silently masking
    as ``None`` in the gather result. Production swallows the error
    branches in its own ``try/except``; anything reaching the gather
    here is a bug we want surfaced.
    """
    pending: list[asyncio.Task] = controller._spawned_tasks  # type: ignore[attr-defined]
    if pending:
        await asyncio.gather(*pending)
        pending.clear()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


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
        "esphome_device_builder.controllers.devices.storage_regen.create_subprocess_exec",
        _fake_spawn,
    )
    persist_calls: list[str] = []

    async def _fake_finalize(_self: Any, configuration: str) -> None:
        persist_calls.append(configuration)

    monkeypatch.setattr(DevicesController, "_finalize_regen_success", _fake_finalize)

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
    assert controller.state.regen.pending == set()


async def test_out_of_band_network_edit_spawns_only_generate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A scanner UPDATED with a moved network block regenerates StorageJSON (#2486)."""
    controller = make_controller(
        tmp_path,
        with_state_monitor=True,
        with_regenerate_state=True,
        esphome_cmd=["esphome"],
    )
    captured_cmd: list[list[str]] = []

    async def _fake_spawn(*args: str, **_kwargs: Any) -> _FakeProc:
        captured_cmd.append(list(args))
        return _FakeProc(returncode=0)

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen.create_subprocess_exec",
        _fake_spawn,
    )
    monkeypatch.setattr(DevicesController, "_finalize_regen_success", AsyncMock())
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    _seed_store(controller, "kitchen.yaml", network_fingerprint="pre-edit-digest")

    controller._on_scan_change(
        ScanChange.UPDATED,
        make_device(name="kitchen", network_fingerprint="post-edit-digest"),
    )
    await _drain(controller)

    assert captured_cmd == [
        [
            "esphome",
            "--dashboard",
            "compile",
            "--only-generate",
            str(tmp_path / "kitchen.yaml"),
        ]
    ]


# ---------------------------------------------------------------------------
# Early-return guards
# ---------------------------------------------------------------------------


def test_regenerate_skips_when_esphome_cmd_unset(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """``_esphome_cmd`` empty (``start()`` hasn't run) → no-op.

    Synchronous test — the guard is the very first check in the
    function and short-circuits before scheduling the
    background task. No spawn, no ``state.regen.pending`` mutation.
    """
    # ``esphome_cmd=[]`` triggers the early-return guard.
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=[])

    controller._schedule_storage_regenerate("kitchen.yaml")

    assert controller._spawned_tasks == []  # type: ignore[attr-defined]
    assert controller.state.regen.pending == set()


def test_regenerate_skips_secrets_yaml(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """secrets.yaml is shared credentials, not a buildable config; regen is a no-op.

    Even with esphome_cmd set (so a real config would schedule), secrets.yaml
    has no build dir to --only-generate, so nothing is spawned.
    """
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])

    controller._schedule_storage_regenerate("secrets.yaml")

    assert controller._spawned_tasks == []  # type: ignore[attr-defined]
    assert controller.state.regen.pending == set()


def test_regenerate_skips_duplicate_schedule(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Configuration already in ``state.regen.pending`` → second schedule is a no-op.

    Without this, repeated saves while a regenerate is already
    in flight would queue N background tasks all racing on the
    same YAML.
    """
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])
    controller.state.regen.pending.add("kitchen.yaml")

    controller._schedule_storage_regenerate("kitchen.yaml")

    assert controller._spawned_tasks == []  # type: ignore[attr-defined]


async def test_regenerate_skips_when_stamp_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A fresh stamp with the budget spent → no spawn.

    The stamp releases when the YAML's mtime moves (the user
    edited it) or its TTL expires; until then a respin would just
    burn another subprocess on the same bad input.
    """
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])
    (tmp_path / "kitchen.yaml").write_text("not: valid: yaml\n", encoding="utf-8")
    _seed_attempts(controller, "kitchen.yaml", storage_regen._MAX_REGEN_ATTEMPTS, age=300.0)
    spawn_calls: list[tuple[str, ...]] = []

    async def _fake_spawn(*args: str, **_kwargs: Any) -> _FakeProc:
        spawn_calls.append(args)
        return _FakeProc(returncode=0)

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen.create_subprocess_exec",
        _fake_spawn,
    )

    controller._schedule_storage_regenerate("kitchen.yaml")
    await _drain(controller)

    assert spawn_calls == []
    # Only a TTL re-check timer is armed: no scan event fires for an
    # untouched file, so expiry is the re-arm signal.
    handle = controller.state.regen.retry_timers["kitchen.yaml"]
    remaining = handle.when() - asyncio.get_running_loop().time()
    ttl = storage_regen._REGEN_FAILURE_TTL_SECONDS
    assert ttl - 301.0 < remaining <= ttl - 299.0
    controller.state.regen.cancel_all_retry_timers()


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


async def test_regenerate_marks_failed_on_nonzero_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """Non-zero exit on the last attempt → no reload, terminal stamp.

    Captures the typical "user saved a YAML with a syntax error"
    case. The stamped budget has to survive so the next sight of
    the same broken YAML doesn't re-spawn.
    """
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])
    (tmp_path / "kitchen.yaml").write_text("not: valid: yaml\n", encoding="utf-8")
    # Attempt 3 recorded 200s ago: past its 120s backoff, one attempt left.
    _seed_attempts(controller, "kitchen.yaml", 3, age=200.0)

    async def _fake_spawn(*_args: str, **_kwargs: Any) -> _FakeProc:
        return _FakeProc(returncode=1, stderr=b"YAML parse error at line 3")

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen.create_subprocess_exec",
        _fake_spawn,
    )
    persist_calls: list[str] = []

    async def _fake_persist(_self: Any, configuration: str) -> None:
        persist_calls.append(configuration)

    monkeypatch.setattr(DevicesController, "_persist_expected_config_hash", _fake_persist)

    controller._schedule_storage_regenerate("kitchen.yaml")
    await _drain(controller)

    # Failure → reload skipped, persist skipped, budget spent on record.
    assert not any(c[0] == "reload" for c in controller._scanner.calls)
    assert persist_calls == []
    md = _read_store(controller, "kitchen.yaml")
    assert md["regen_failed_attempts"] == storage_regen._MAX_REGEN_ATTEMPTS
    # Terminal → only the TTL re-check timer remains.
    assert set(controller.state.regen.retry_timers) == {"kitchen.yaml"}
    controller.state.regen.cancel_all_retry_timers()
    # Pending cleared via the ``finally``.
    assert controller.state.regen.pending == set()


async def test_regenerate_marks_failed_on_spawn_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """Spawn raising on the last attempt → terminal stamp.

    Triggers when ``esphome`` is missing from PATH (broken pip
    install, dashboard running outside its venv). The pending
    marker has to clear via the outer ``finally`` so a follow-up
    schedule on the same configuration isn't blocked by the
    duplicate-schedule guard.
    """
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    _seed_attempts(controller, "kitchen.yaml", 3, age=200.0)

    async def _broken_spawn(*_args: str, **_kwargs: Any) -> _FakeProc:
        raise OSError("esphome: command not found")

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen.create_subprocess_exec",
        _broken_spawn,
    )
    monkeypatch.setattr(
        DevicesController,
        "_persist_expected_config_hash",
        AsyncMock(),
    )

    controller._schedule_storage_regenerate("kitchen.yaml")
    await _drain(controller)

    assert not any(c[0] == "reload" for c in controller._scanner.calls)
    md = _read_store(controller, "kitchen.yaml")
    assert md["regen_failed_attempts"] == storage_regen._MAX_REGEN_ATTEMPTS
    assert set(controller.state.regen.retry_timers) == {"kitchen.yaml"}
    controller.state.regen.cancel_all_retry_timers()
    assert controller.state.regen.pending == set()


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


async def test_regenerate_dedupes_same_tick_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """
    Two ``_schedule_storage_regenerate`` calls in the same tick → one task.

    Pins the pre-yield window the in-flight test below can't
    reach; the second sync call has to see
    ``state.regen.pending`` populated before the spawned
    coroutine runs.
    """
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])

    async def _fake_spawn(*_args: str, **_kwargs: Any) -> _FakeProc:
        return _FakeProc(returncode=0)

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen.create_subprocess_exec",
        _fake_spawn,
    )
    monkeypatch.setattr(
        DevicesController,
        "_finalize_regen_success",
        AsyncMock(),
    )

    # Two synchronous calls — no ``await`` between them, so the
    # spawned coroutine hasn't had a chance to run.
    controller._schedule_storage_regenerate("kitchen.yaml")
    controller._schedule_storage_regenerate("kitchen.yaml")

    assert len(controller._spawned_tasks) == 1  # type: ignore[attr-defined]
    # Sync ``.add()`` in ``schedule`` is the load-bearing piece —
    # without it the second call wouldn't see the marker yet.
    assert controller.state.regen.pending == {"kitchen.yaml"}

    await _drain(controller)
    assert controller.state.regen.pending == set()


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
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    in_flight = asyncio.Event()
    release = asyncio.Event()

    async def _hold(*_args: str, **_kwargs: Any) -> _FakeProc:
        in_flight.set()
        await release.wait()
        return _FakeProc(returncode=0)

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen.create_subprocess_exec",
        _hold,
    )
    monkeypatch.setattr(
        DevicesController,
        "_persist_expected_config_hash",
        AsyncMock(),
    )

    controller._schedule_storage_regenerate("kitchen.yaml")
    await asyncio.wait_for(in_flight.wait(), timeout=2.0)
    assert controller.state.regen.pending == {"kitchen.yaml"}

    # Second schedule while the first is still inside ``communicate``.
    controller._schedule_storage_regenerate("kitchen.yaml")
    # Only the original task exists.
    assert len(controller._spawned_tasks) == 1  # type: ignore[attr-defined]

    release.set()
    await _drain(controller)
    assert controller.state.regen.pending == set()


# ---------------------------------------------------------------------------
# Cross-restart failure persistence
# ---------------------------------------------------------------------------


async def test_regenerate_persists_mtime_and_wallclock_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """Budget-spent failure → YAML mtime + wall-clock stamped into the sidecar.

    The whole point of the cross-restart guard: a backend reboot
    that re-encounters the same broken YAML reads these stamps,
    sees the mtime hasn't moved AND the failure is fresh, and
    skips replaying the regen. The wall-clock side feeds the
    TTL — without it, a persistent external problem would never
    get re-checked.
    """
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("not: valid: yaml\n", encoding="utf-8")
    expected_mtime = yaml_path.stat().st_mtime

    async def _fake_spawn(*_args: str, **_kwargs: Any) -> _FakeProc:
        return _FakeProc(returncode=1, stderr=b"YAML parse error")

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen.create_subprocess_exec",
        _fake_spawn,
    )
    monkeypatch.setattr(
        DevicesController,
        "_persist_expected_config_hash",
        AsyncMock(),
    )
    # Pin wall-clock so the assertion isn't racy.
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen.time.time",
        lambda: 1700000000.0,
    )

    controller._schedule_storage_regenerate("kitchen.yaml")
    await _drain(controller)

    md = _read_store(controller, "kitchen.yaml")
    assert md.get("regen_failed_mtime") == expected_mtime
    assert md.get("regen_failed_at") == 1700000000.0
    assert md.get("regen_failed_attempts") == 1
    controller.state.regen.cancel_all_retry_timers()


async def test_regenerate_persists_stamp_on_spawn_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """Spawn-raises path also stamps the failure marker once the budget is spent.

    Both failure exits — non-zero returncode and ``OSError`` from
    the spawn itself — feed the same persistent guard. Catches
    regressions where one branch persists and the other doesn't.
    """
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    expected_mtime = yaml_path.stat().st_mtime

    async def _broken_spawn(*_args: str, **_kwargs: Any) -> _FakeProc:
        raise OSError("esphome: command not found")

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen.create_subprocess_exec",
        _broken_spawn,
    )
    monkeypatch.setattr(
        DevicesController,
        "_persist_expected_config_hash",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen.time.time",
        lambda: 1700000050.0,
    )

    controller._schedule_storage_regenerate("kitchen.yaml")
    await _drain(controller)

    md = _read_store(controller, "kitchen.yaml")
    assert md.get("regen_failed_mtime") == expected_mtime
    assert md.get("regen_failed_at") == 1700000050.0
    assert md.get("regen_failed_attempts") == 1
    controller.state.regen.cancel_all_retry_timers()


async def test_regenerate_clears_failure_stamp_on_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A subsequent successful regen wipes both halves of the failure stamp.

    User edits the broken YAML → mtime moves → the next schedule
    bypasses the cross-restart guard, the spawn succeeds, and the
    stale ``regen_failed_mtime`` *and* ``regen_failed_at`` get
    cleared so a future restart doesn't see them. Pairs with the
    failure-persistence test above to pin the full set/clear cycle.
    """
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    # Simulate the leftover stamp from an earlier failed attempt.
    _seed_store(
        controller,
        "kitchen.yaml",
        regen_failed_mtime=1.0,
        regen_failed_at=1700000000.0,
    )

    async def _fake_spawn(*_args: str, **_kwargs: Any) -> _FakeProc:
        return _FakeProc(returncode=0)

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen.create_subprocess_exec",
        _fake_spawn,
    )
    monkeypatch.setattr(
        DevicesController,
        "_persist_expected_config_hash",
        AsyncMock(),
    )

    controller._schedule_storage_regenerate("kitchen.yaml")
    await _drain(controller)

    md = _read_store(controller, "kitchen.yaml")
    assert "regen_failed_mtime" not in md
    assert "regen_failed_at" not in md


async def test_regenerate_retries_when_stamp_older_than_ttl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """TTL elapsed: even with mtime untouched, the next restart retries.

    Covers the user's "external package problem" case — a flaky
    git server or ESPHome update that resolves on its own. The
    YAML doesn't change but enough wall-clock time has passed
    that we should re-check rather than blocking forever.
    """
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    current_mtime = yaml_path.stat().st_mtime
    _seed_store(
        controller,
        "kitchen.yaml",
        regen_failed_mtime=current_mtime,
        regen_failed_at=1700000000.0,
    )
    # Advance the clock just past the TTL.
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen.time.time",
        lambda: 1700000000.0 + storage_regen._REGEN_FAILURE_TTL_SECONDS + 100.0,
    )

    spawn_calls: list[tuple[str, ...]] = []

    async def _fake_spawn(*args: str, **_kwargs: Any) -> _FakeProc:
        spawn_calls.append(args)
        return _FakeProc(returncode=0)

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen.create_subprocess_exec",
        _fake_spawn,
    )
    monkeypatch.setattr(
        DevicesController,
        "_persist_expected_config_hash",
        AsyncMock(),
    )

    controller._schedule_storage_regenerate("kitchen.yaml")
    await _drain(controller)

    assert len(spawn_calls) == 1


async def test_regenerate_runs_when_yaml_mtime_moves_past_stamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """User edits the broken YAML → mtime moves → cross-restart guard releases.

    The natural retry signal. Without this the user's only escape
    from a bad regen would be deleting the metadata sidecar by
    hand, which they can't reasonably be expected to know about.
    """
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    # Stamp from a prior failed attempt at an *older* mtime — the
    # YAML has since been edited so the live stat doesn't match.
    # The wall-clock stamp is fresh (within TTL) so only the mtime
    # mismatch is what releases the guard.
    _seed_store(
        controller,
        "kitchen.yaml",
        regen_failed_mtime=1.0,
        regen_failed_at=1700000000.0,
    )
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen.time.time",
        lambda: 1700000060.0,
    )

    spawn_calls: list[tuple[str, ...]] = []

    async def _fake_spawn(*args: str, **_kwargs: Any) -> _FakeProc:
        spawn_calls.append(args)
        return _FakeProc(returncode=0)

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen.create_subprocess_exec",
        _fake_spawn,
    )
    monkeypatch.setattr(
        DevicesController,
        "_persist_expected_config_hash",
        AsyncMock(),
    )

    controller._schedule_storage_regenerate("kitchen.yaml")
    await _drain(controller)

    assert len(spawn_calls) == 1


async def test_regenerate_skips_when_yaml_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """YAML vanished between scan and schedule → no spawn against it.

    A torn ``stat()`` (file removed mid-flight by an editor or
    archive) returns ``OSError``; a spawn would just fail against
    the missing file, so the run skips and the next scan event
    for the path retriggers.
    """
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])
    spawn_calls: list[tuple[str, ...]] = []

    async def _fake_spawn(*args: str, **_kwargs: Any) -> _FakeProc:
        spawn_calls.append(args)
        return _FakeProc(returncode=1, stderr=b"missing")

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen.create_subprocess_exec",
        _fake_spawn,
    )

    controller._schedule_storage_regenerate("kitchen.yaml")
    await _drain(controller)

    assert spawn_calls == []
    assert controller.state.regen.pending == set()


async def test_regenerate_clamps_negative_stamp_age(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A future-dated ``regen_failed_at`` (clock skew, NTP step) is clamped to "fresh".

    Without the ``max(0.0, ...)`` clamp, ``time.time() -
    cached_at`` could be a large negative number — still less
    than the TTL, so the guard would correctly skip the regen,
    but only by accident of float comparison semantics. Pin the
    clamp explicitly so a future refactor that drops it doesn't
    silently change the contract.
    """
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("not: valid: yaml\n", encoding="utf-8")
    current_mtime = yaml_path.stat().st_mtime
    # Stamp claims the failure happened *in the future* (roughly year 2033).
    _seed_store(
        controller,
        "kitchen.yaml",
        regen_failed_mtime=current_mtime,
        regen_failed_at=2_000_000_000.0,
    )
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen.time.time",
        lambda: 1700000000.0,
    )

    spawn_calls: list[tuple[str, ...]] = []

    async def _fake_spawn(*args: str, **_kwargs: Any) -> _FakeProc:
        spawn_calls.append(args)
        return _FakeProc(returncode=0)

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen.create_subprocess_exec",
        _fake_spawn,
    )

    controller._schedule_storage_regenerate("kitchen.yaml")
    await _drain(controller)

    assert spawn_calls == []


async def test_regenerate_runs_when_only_one_stamp_half_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """Half-written sidecar (only mtime, only wall-clock) → guard treats as absent.

    The two stamp halves are written together; any state where
    only one is present came from a partial write or a hand-edit
    and shouldn't lock out retries indefinitely. Both-or-neither
    is the contract the guard enforces.
    """
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    current_mtime = yaml_path.stat().st_mtime
    # Only the mtime half — sidecar carries no wall-clock pair.
    _seed_store(
        controller,
        "kitchen.yaml",
        regen_failed_mtime=current_mtime,
    )

    spawn_calls: list[tuple[str, ...]] = []

    async def _fake_spawn(*args: str, **_kwargs: Any) -> _FakeProc:
        spawn_calls.append(args)
        return _FakeProc(returncode=0)

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen.create_subprocess_exec",
        _fake_spawn,
    )
    monkeypatch.setattr(
        DevicesController,
        "_persist_expected_config_hash",
        AsyncMock(),
    )

    controller._schedule_storage_regenerate("kitchen.yaml")
    await _drain(controller)

    assert len(spawn_calls) == 1


async def test_regenerate_runs_when_stamp_has_corrupt_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A non-numeric stamp half (hand-edit, partial write) is treated as absent.

    Production stamps via ``set_device_metadata`` only — but a
    user editing ``.device-builder.json`` could leave the field as
    a string or arbitrary object. The guard's ``float(...)``
    coercion has to recover gracefully; otherwise a single bad
    write would lock the device out of regen forever.
    """
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    # Hand-edit shape — the value is a string, not a number.
    raw_path = tmp_path / ".device-builder.json"
    raw_path.write_text(
        '{"kitchen.yaml": {"regen_failed_mtime": "garbage", "regen_failed_at": "garbage"}}',
        encoding="utf-8",
    )

    spawn_calls: list[tuple[str, ...]] = []

    async def _fake_spawn(*args: str, **_kwargs: Any) -> _FakeProc:
        spawn_calls.append(args)
        return _FakeProc(returncode=0)

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen.create_subprocess_exec",
        _fake_spawn,
    )
    monkeypatch.setattr(
        DevicesController,
        "_persist_expected_config_hash",
        AsyncMock(),
    )

    controller._schedule_storage_regenerate("kitchen.yaml")
    await _drain(controller)

    assert len(spawn_calls) == 1


# ---------------------------------------------------------------------------
# Real ``_finalize_regen_success`` — covers the in-executor closure that
# reads ``build_info.json`` and writes the sidecar in one transaction.
# ---------------------------------------------------------------------------


async def test_regenerate_persists_hash_and_clears_stamp_in_one_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """Success path runs the real ``_finalize_regen_success`` end-to-end.

    Other tests in this file mock the finalize helper to verify that
    the spawn-success branch invokes it; this one lets it execute so
    the in-executor closure (``read_build_info_hash`` →
    ``set_device_metadata``) gets exercised against real fixtures.
    Asserts the sidecar after the run carries the canonical hash AND
    has the leftover regen-failure stamp cleared in the same write.
    """
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n", encoding="utf-8")

    # Pre-seed a leftover failure stamp from a notional prior backend
    # so the test can verify it's cleared in the same transaction
    # that writes the new hash.
    _seed_store(
        controller,
        "kitchen.yaml",
        regen_failed_mtime=1.0,
        regen_failed_at=2.0,
    )

    # StorageJSON sidecar pointing at a build dir + build_info.json
    # carrying the canonical hash. ``read_build_info_hash`` reads
    # both during the executor closure.
    build_path = tmp_path / ".esphome" / "build" / "kitchen"
    write_storage_json(
        tmp_path,
        "kitchen.yaml",
        firmware_bin_path=build_path / ".pioenvs" / "firmware.bin",
        build_path=build_path,
    )
    build_path.mkdir(parents=True, exist_ok=True)
    (build_path / "build_info.json").write_text(
        # 0x5a94a12d — same hash the metadata-resolver tests use,
        # matches ``acfloatmonitor32.yaml``'s post-codegen value.
        '{"config_hash": 1519690029, "build_time": 1700000000, '
        '"build_time_str": "2025-11-14 12:00:00", '
        '"esphome_version": "2026.5.0-dev"}',
        encoding="utf-8",
    )

    async def _fake_spawn(*_args: str, **_kwargs: Any) -> _FakeProc:
        return _FakeProc(returncode=0)

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen.create_subprocess_exec",
        _fake_spawn,
    )

    controller._schedule_storage_regenerate("kitchen.yaml")
    await _drain(controller)

    md = _read_store(controller, "kitchen.yaml")
    # Hash got written and the leftover stamps got cleared in the
    # same transaction — both halves of the closure.
    assert md.get("expected_config_hash") == "5a94a12d"
    assert "regen_failed_mtime" not in md
    assert "regen_failed_at" not in md


async def test_regenerate_success_clears_stamp_when_build_info_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Success spawn but no ``build_info.json`` → log warn, still clear stamps.

    Pins the "missing build_info.json" branch in
    ``_finalize_regen_success``: when ``read_build_info_hash``
    returns ``None`` the closure still writes the cleared regen
    stamps (so the next restart picks up the now-good YAML), and
    the caller logs a warning rather than silently dropping the
    case.
    """
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n", encoding="utf-8")

    _seed_store(
        controller,
        "kitchen.yaml",
        regen_failed_mtime=1.0,
        regen_failed_at=2.0,
    )

    # No StorageJSON sidecar → ``read_build_info_hash`` returns
    # None.

    async def _fake_spawn(*_args: str, **_kwargs: Any) -> _FakeProc:
        return _FakeProc(returncode=0)

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen.create_subprocess_exec",
        _fake_spawn,
    )

    with caplog.at_level(
        logging.WARNING,
        logger="esphome_device_builder.controllers.devices.controller",
    ):
        controller._schedule_storage_regenerate("kitchen.yaml")
        await _drain(controller)

    md = _read_store(controller, "kitchen.yaml")
    # Stamps cleared even though the hash couldn't be read — the
    # YAML now generates cleanly, the missing build_info.json is
    # a separate concern surfaced by the warning log.
    assert "expected_config_hash" not in md
    assert "regen_failed_mtime" not in md
    assert "regen_failed_at" not in md
    assert any(
        "Could not read config_hash from build_info.json" in record.message
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# Bounded retry before the stamp
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attempts_value", "expected_attempts"),
    [
        pytest.param(
            storage_regen._MAX_REGEN_ATTEMPTS, storage_regen._MAX_REGEN_ATTEMPTS, id="terminal"
        ),
        pytest.param(None, storage_regen._MAX_REGEN_ATTEMPTS, id="legacy-missing"),
        pytest.param("garbage", storage_regen._MAX_REGEN_ATTEMPTS, id="corrupt"),
        pytest.param(0, storage_regen._MAX_REGEN_ATTEMPTS, id="nonpositive"),
        pytest.param(2, 2, id="mid-ladder"),
    ],
)
def test_fresh_stamp_attempts_read(attempts_value: Any, expected_attempts: int) -> None:
    """A missing, unparseable, or non-positive attempt count reads as terminal."""
    md: dict[str, Any] = {"regen_failed_mtime": 5.0, "regen_failed_at": 1000.0}
    if attempts_value is not None:
        md["regen_failed_attempts"] = attempts_value
    assert storage_regen._fresh_stamp(md, 5.0, 1060.0) == (expected_attempts, 60.0)


@pytest.mark.parametrize(
    ("md", "current_mtime", "now"),
    [
        pytest.param({}, 5.0, 1060.0, id="no-stamp"),
        pytest.param(
            {"regen_failed_mtime": 1.0, "regen_failed_at": 1000.0}, 5.0, 1060.0, id="mtime-moved"
        ),
        pytest.param(
            {"regen_failed_mtime": 5.0, "regen_failed_at": 1000.0},
            5.0,
            1000.0 + storage_regen._REGEN_FAILURE_TTL_SECONDS + 1,
            id="expired",
        ),
        pytest.param(
            {"regen_failed_mtime": "garbage", "regen_failed_at": "garbage"},
            5.0,
            1060.0,
            id="corrupt-halves",
        ),
    ],
)
def test_fresh_stamp_ignores_invalid_stamps(
    md: dict[str, Any], current_mtime: float, now: float
) -> None:
    """A missing, stale, expired, or unparseable stamp reads as no stamp."""
    assert storage_regen._fresh_stamp(md, current_mtime, now) is None


def test_fresh_stamp_clamps_future_dated_stamp() -> None:
    """Clock skew can't lock the regen out: a future stamp clamps to age 0."""
    md = {"regen_failed_mtime": 5.0, "regen_failed_at": 2000.0, "regen_failed_attempts": 1}
    assert storage_regen._fresh_stamp(md, 5.0, 1000.0) == (1, 0.0)


class _HangingProc:
    """Subprocess stand-in whose ``communicate`` blocks until killed."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.killed = False
        self._blocked = asyncio.Event()

    async def communicate(self) -> tuple[bytes, bytes]:
        await self._blocked.wait()
        return (b"", b"")

    def kill(self) -> None:
        self.killed = True


async def test_ttl_expiry_grants_fresh_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A terminal stamp past its TTL runs again, and the next failure counts from 1."""
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])
    (tmp_path / "kitchen.yaml").write_text("not: valid: yaml\n", encoding="utf-8")
    _seed_attempts(
        controller,
        "kitchen.yaml",
        storage_regen._MAX_REGEN_ATTEMPTS,
        age=storage_regen._REGEN_FAILURE_TTL_SECONDS + 100.0,
    )

    async def _fake_spawn(*_args: str, **_kwargs: Any) -> _FakeProc:
        return _FakeProc(returncode=1, stderr=b"YAML parse error")

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen.create_subprocess_exec",
        _fake_spawn,
    )

    controller._schedule_storage_regenerate("kitchen.yaml")
    await _drain(controller)

    assert _read_store(controller, "kitchen.yaml")["regen_failed_attempts"] == 1
    assert "kitchen.yaml" in controller.state.regen.retry_timers
    controller.state.regen.cancel_all_retry_timers()


async def test_restart_inside_backoff_rearms_remainder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A fresh non-terminal stamp inside its backoff re-arms the remainder, no spawn."""
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    # Attempt 1 recorded 10s ago: 20s of its 30s backoff remain.
    _seed_attempts(controller, "kitchen.yaml", 1, age=10.0)
    spawn_calls: list[tuple[str, ...]] = []

    async def _fake_spawn(*args: str, **_kwargs: Any) -> _FakeProc:
        spawn_calls.append(args)
        return _FakeProc(returncode=0)

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen.create_subprocess_exec",
        _fake_spawn,
    )

    controller._schedule_storage_regenerate("kitchen.yaml")
    await _drain(controller)

    assert spawn_calls == []
    handle = controller.state.regen.retry_timers["kitchen.yaml"]
    remaining = handle.when() - asyncio.get_running_loop().time()
    assert 15.0 < remaining <= 20.5
    controller.state.regen.cancel_all_retry_timers()


async def test_backoff_elapsed_runs_next_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A fresh non-terminal stamp past its backoff spawns the next attempt."""
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])
    (tmp_path / "kitchen.yaml").write_text("not: valid: yaml\n", encoding="utf-8")
    _seed_attempts(controller, "kitchen.yaml", 1, age=40.0)
    spawn_calls: list[tuple[str, ...]] = []

    async def _fake_spawn(*args: str, **_kwargs: Any) -> _FakeProc:
        spawn_calls.append(args)
        return _FakeProc(returncode=1, stderr=b"YAML parse error")

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen.create_subprocess_exec",
        _fake_spawn,
    )

    controller._schedule_storage_regenerate("kitchen.yaml")
    await _drain(controller)

    assert len(spawn_calls) == 1
    assert _read_store(controller, "kitchen.yaml")["regen_failed_attempts"] == 2
    controller.state.regen.cancel_all_retry_timers()


async def test_cancelled_run_kills_subprocess_without_stamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A run cancelled mid-subprocess kills the child and records nothing."""
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    proc = _HangingProc()
    started = asyncio.Event()

    async def _fake_spawn(*_args: str, **_kwargs: Any) -> _HangingProc:
        started.set()
        return proc

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen.create_subprocess_exec",
        _fake_spawn,
    )

    controller._schedule_storage_regenerate("kitchen.yaml")
    await asyncio.wait_for(started.wait(), 1)
    await asyncio.sleep(0)  # let the run reach the communicate await
    (task,) = controller._spawned_tasks  # type: ignore[attr-defined]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    controller._spawned_tasks.clear()  # type: ignore[attr-defined]

    assert proc.killed
    assert _read_store(controller, "kitchen.yaml") == {}
    assert controller.state.regen.pending == set()


async def test_communicate_failure_kills_child_and_counts_the_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A pipe error mid-communicate kills the child and feeds the ladder."""
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n", encoding="utf-8")

    class _BrokenPipeProc:
        returncode: int | None = None

        def __init__(self) -> None:
            self.killed = False

        async def communicate(self) -> tuple[bytes, bytes]:
            raise OSError("broken pipe")

        def kill(self) -> None:
            self.killed = True

    proc = _BrokenPipeProc()

    async def _fake_spawn(*_args: str, **_kwargs: Any) -> _BrokenPipeProc:
        return proc

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen.create_subprocess_exec",
        _fake_spawn,
    )

    controller._schedule_storage_regenerate("kitchen.yaml")
    await _drain(controller)

    assert proc.killed
    assert _read_store(controller, "kitchen.yaml")["regen_failed_attempts"] == 1
    controller.state.regen.cancel_all_retry_timers()


async def test_vanished_mid_run_stamps_prespawn_mtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A YAML deleted during the run stamps the pre-spawn mtime; the retry skips."""
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    prespawn_mtime = yaml_path.stat().st_mtime

    async def _fake_spawn(*_args: str, **_kwargs: Any) -> _FakeProc:
        # Off-loop: blockbuster flags blocking unlink on the loop (Linux CI).
        await asyncio.to_thread(yaml_path.unlink)
        return _FakeProc(returncode=1, stderr=b"gone")

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen.create_subprocess_exec",
        _fake_spawn,
    )

    controller._schedule_storage_regenerate("kitchen.yaml")
    await _drain(controller)

    md = _read_store(controller, "kitchen.yaml")
    assert md["regen_failed_attempts"] == 1
    assert md["regen_failed_mtime"] == prespawn_mtime

    # The armed retry finds the file gone and skips without counting.
    storage_regen._fire_retry(controller, "kitchen.yaml")
    await _drain(controller)

    assert _read_store(controller, "kitchen.yaml")["regen_failed_attempts"] == 1
    assert controller.state.regen.retry_timers == {}


async def test_regenerate_warns_when_yaml_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-vanish stat error skips loudly instead of masquerading as a vanish."""
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])
    spawn_calls: list[tuple[str, ...]] = []

    async def _fake_spawn(*args: str, **_kwargs: Any) -> _FakeProc:
        spawn_calls.append(args)
        return _FakeProc(returncode=0)

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen.create_subprocess_exec",
        _fake_spawn,
    )

    async def _denied(_fn: Any) -> float:
        raise PermissionError("denied")

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen.run_in_executor", _denied
    )

    with caplog.at_level(logging.WARNING):
        controller._schedule_storage_regenerate("kitchen.yaml")
        await _drain(controller)

    assert spawn_calls == []
    assert any("config unreadable" in record.message for record in caplog.records)
    assert controller.state.regen.pending == set()


async def test_edit_between_failures_resets_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """An mtime move between failures restarts the ladder at attempt 1."""
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n", encoding="utf-8")

    async def _fake_spawn(*_args: str, **_kwargs: Any) -> _FakeProc:
        return _FakeProc(returncode=2, stderr=b"fatal: could not clone")

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen.create_subprocess_exec",
        _fake_spawn,
    )

    controller._schedule_storage_regenerate("kitchen.yaml")
    await _drain(controller)
    assert _read_store(controller, "kitchen.yaml")["regen_failed_attempts"] == 1

    # The edit moves the mtime; the manually fired retry finds a stale
    # stamp and the next failure counts from 1 with the new mtime.
    old_mtime = yaml_path.stat().st_mtime
    os.utime(yaml_path, (old_mtime + 10, old_mtime + 10))
    storage_regen._fire_retry(controller, "kitchen.yaml")
    await _drain(controller)

    md = _read_store(controller, "kitchen.yaml")
    assert md["regen_failed_attempts"] == 1
    assert md["regen_failed_mtime"] == old_mtime + 10
    controller.state.regen.cancel_all_retry_timers()


async def test_regenerate_first_failure_stamps_and_arms_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A first failure records attempt 1 and arms a backoff retry."""
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n", encoding="utf-8")

    async def _fake_spawn(*_args: str, **_kwargs: Any) -> _FakeProc:
        return _FakeProc(returncode=2, stderr=b"fatal: could not clone")

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen.create_subprocess_exec",
        _fake_spawn,
    )

    controller._schedule_storage_regenerate("kitchen.yaml")
    await _drain(controller)

    assert _read_store(controller, "kitchen.yaml")["regen_failed_attempts"] == 1
    assert "kitchen.yaml" in controller.state.regen.retry_timers
    controller.state.regen.cancel_all_retry_timers()


async def test_regenerate_retry_fires_and_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """The armed retry re-runs the regen; a success clears the retry state."""
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen._RETRY_BACKOFF_BASE_SECONDS",
        0.0,
    )
    outcomes = [_FakeProc(returncode=2, stderr=b"fatal: clone interrupted"), _FakeProc()]

    async def _fake_spawn(*_args: str, **_kwargs: Any) -> _FakeProc:
        return outcomes.pop(0)

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen.create_subprocess_exec",
        _fake_spawn,
    )
    monkeypatch.setattr(DevicesController, "_finalize_regen_success", AsyncMock())

    controller._schedule_storage_regenerate("kitchen.yaml")
    # The success cycle's scanner reload is the last observable step.
    await wait_until(
        lambda: any(c[0] == "reload" for c in controller._scanner.calls), 1, "scanner reload"
    )
    await _drain(controller)

    assert outcomes == []
    assert controller.state.regen.retry_timers == {}
    reload_calls = [c for c in controller._scanner.calls if c[0] == "reload"]
    assert reload_calls == [("reload", "kitchen.yaml")]


async def test_regenerate_retry_budget_exhausts_to_stamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Persistent failure spends the budget, then stamps and marks failed."""
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])
    (tmp_path / "kitchen.yaml").write_text("not: valid: yaml\n", encoding="utf-8")
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen._RETRY_BACKOFF_BASE_SECONDS",
        0.0,
    )
    spawns = 0

    async def _fake_spawn(*_args: str, **_kwargs: Any) -> _FakeProc:
        nonlocal spawns
        spawns += 1
        return _FakeProc(returncode=1, stderr=b"YAML parse error")

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen.create_subprocess_exec",
        _fake_spawn,
    )

    with caplog.at_level(logging.WARNING):
        controller._schedule_storage_regenerate("kitchen.yaml")
        # The terminal attempt count is the last step of the exhausted path.
        await wait_until(
            lambda: (
                _read_store(controller, "kitchen.yaml").get("regen_failed_attempts", 0)
                >= storage_regen._MAX_REGEN_ATTEMPTS
            ),
            1,
            "terminal stamp",
        )
        await _drain(controller)

    assert spawns == storage_regen._MAX_REGEN_ATTEMPTS
    # The give-up warning carries the failure summary.
    giveup = [r for r in caplog.records if "failed 4 times" in r.getMessage()]
    assert giveup and "exit 1: YAML parse error" in giveup[0].getMessage()
    # A TTL re-check timer covers in-session expiry.
    assert set(controller.state.regen.retry_timers) == {"kitchen.yaml"}
    controller.state.regen.cancel_all_retry_timers()


async def test_stop_cancels_armed_retries_without_stamping(
    tmp_path: Path, make_db: MakeDbFactory
) -> None:
    """``stop()`` cancels armed retries and writes nothing.

    The failure that armed the retry already stamped its attempt
    count, so a restart resumes the ladder from the store.
    """
    controller = DevicesController(make_db(tmp_path))
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    handle = asyncio.get_running_loop().call_later(30.0, lambda: None)
    controller.state.regen.retry_timers["kitchen.yaml"] = handle

    with (
        patch.multiple(controller._state_monitor, stop=AsyncMock()),
        patch.multiple(controller._mqtt_coordinator, stop=AsyncMock()),
        patch.multiple(controller._build_size, stop=AsyncMock()),
        patch.multiple(controller._scanner, stop=AsyncMock()),
    ):
        await controller.stop()

    assert handle.cancelled()
    assert controller.state.regen.retry_timers == {}
    assert controller._metadata_store.get("kitchen.yaml") == {}


async def test_arm_retry_replaces_and_cancels_prior_handle() -> None:
    """Re-arming for the same configuration cancels the prior timer."""
    state = RegenState()
    loop = asyncio.get_running_loop()
    first = loop.call_later(30.0, lambda: None)
    second = loop.call_later(30.0, lambda: None)

    state.arm_retry("kitchen.yaml", first)
    state.arm_retry("kitchen.yaml", second)

    assert first.cancelled()
    assert not second.cancelled()
    assert state.retry_timers == {"kitchen.yaml": second}
    state.cancel_all_retry_timers()


async def test_cancel_all_retry_timers_cancels_pending_handles() -> None:
    """``stop()``'s mass-cancel leaves no live retry timer behind."""
    state = RegenState()
    loop = asyncio.get_running_loop()
    handles = [loop.call_later(30.0, lambda: None) for _ in range(2)]
    state.retry_timers = {"a.yaml": handles[0], "b.yaml": handles[1]}

    state.cancel_all_retry_timers()

    assert all(handle.cancelled() for handle in handles)
    assert state.retry_timers == {}
