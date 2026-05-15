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
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from esphome_device_builder.controllers.devices import DevicesController
from tests._storage_fixtures import write_storage_json

from .conftest import MakeControllerFactory


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
    assert controller.state.regenerate_pending == set()
    # Success → not in failed set.
    assert controller.state.regenerate_failed == set()


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
    assert controller.state.regenerate_pending == set()


def test_regenerate_skips_duplicate_schedule(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Configuration already in ``_regenerate_pending`` → second schedule is a no-op.

    Without this, repeated saves while a regenerate is already
    in flight would queue N background tasks all racing on the
    same YAML.
    """
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])
    controller.state.regenerate_pending.add("kitchen.yaml")

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
    controller.state.regenerate_failed.add("kitchen.yaml")

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
        "esphome_device_builder.controllers.devices.storage_regen.create_subprocess_exec",
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
    assert controller.state.regenerate_failed == {"kitchen.yaml"}
    # Pending cleared via the ``finally``.
    assert controller.state.regenerate_pending == set()


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
        "esphome_device_builder.controllers.devices.storage_regen.create_subprocess_exec",
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
    assert controller.state.regenerate_failed == {"kitchen.yaml"}
    assert controller.state.regenerate_pending == set()


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regenerate_dedupes_same_tick_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """
    Two ``_schedule_storage_regenerate`` calls in the same tick → one task.

    Pins the pre-yield window the in-flight test below can't
    reach; the second sync call has to see
    ``_regenerate_pending`` populated before the spawned
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
    assert controller.state.regenerate_pending == {"kitchen.yaml"}

    await _drain(controller)
    assert controller.state.regenerate_pending == set()


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
    assert controller.state.regenerate_pending == {"kitchen.yaml"}

    # Second schedule while the first is still inside ``communicate``.
    controller._schedule_storage_regenerate("kitchen.yaml")
    # Only the original task exists.
    assert len(controller._spawned_tasks) == 1  # type: ignore[attr-defined]

    release.set()
    await _drain(controller)
    assert controller.state.regenerate_pending == set()


# ---------------------------------------------------------------------------
# Cross-restart failure persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regenerate_persists_mtime_and_wallclock_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """Failure → YAML mtime + wall-clock stamped into the metadata sidecar.

    The whole point of the cross-restart guard: a backend reboot
    that re-encounters the same broken YAML reads these stamps,
    sees the mtime hasn't moved AND the failure is fresh, and
    skips replaying the regen. The wall-clock side feeds the
    TTL — without it, a transient external problem (git package
    server flake) would never get re-checked.
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


@pytest.mark.asyncio
async def test_regenerate_persists_stamp_on_spawn_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """Spawn-raises path also stamps the failure marker.

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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_regenerate_skips_when_stamp_fresh_and_mtime_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """Cross-restart skip: persisted stamp matches and is within TTL → no spawn.

    Simulates the "broken config + backend restart" case. The
    in-memory ``_regenerate_failed`` set is empty (fresh process)
    but the sidecar carries the prior backend's failure stamp;
    the schedule call must populate ``_regenerate_failed`` and
    skip the spawn rather than burning another subprocess.
    """
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("not: valid: yaml\n", encoding="utf-8")
    current_mtime = yaml_path.stat().st_mtime
    _seed_store(
        controller,
        "kitchen.yaml",
        regen_failed_mtime=current_mtime,
        regen_failed_at=1700000000.0,
    )
    # 60s after the stamp — well within the 1h TTL.
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

    controller._schedule_storage_regenerate("kitchen.yaml")
    await _drain(controller)

    assert spawn_calls == []
    # The skip path also seeds the in-memory set so subsequent
    # same-session schedules hit the cheaper guard instead of
    # re-reading the sidecar.
    assert controller.state.regenerate_failed == {"kitchen.yaml"}


@pytest.mark.asyncio
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
    # Advance the clock just past the 1h TTL.
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.storage_regen.time.time",
        lambda: 1700000000.0 + 3700.0,
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_regenerate_runs_when_yaml_missing_for_stamp_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """YAML file vanished between scan and schedule → guard is permissive.

    A torn ``stat()`` (file removed mid-flight by an editor or
    archive) returns ``OSError``; the guard treats that as "we
    can't verify, let the spawn try" rather than silently
    skipping. The spawn itself will then fail and re-stamp; the
    important thing is we don't dead-end on a missing file.
    """
    controller = make_controller(tmp_path, with_regenerate_state=True, esphome_cmd=["esphome"])
    # Persist a stamp without ever creating the YAML — simulates the
    # race window. The metadata sidecar's entry is keyed by filename,
    # not file-existence.
    _seed_store(
        controller,
        "kitchen.yaml",
        regen_failed_mtime=12345.0,
        regen_failed_at=1700000000.0,
    )

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

    # The spawn ran (the stat() failed → guard didn't short-circuit).
    assert len(spawn_calls) == 1


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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
