"""Coverage for the receiver-side esphome venv provisioner engine."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from esphome_device_builder.controllers.remote_build.env_provisioner import (
    EnvProvisioner,
    EnvProvisionError,
)
from esphome_device_builder.helpers.subprocess import CapturedSubprocess


class _FakeRunner:
    """Stand-in for ``run_subprocess_capture`` that records calls.

    On the ``venv`` command it creates the target dir (so the readiness
    marker write lands), mirroring what ``python -m venv`` would do. A
    command whose args contain ``fail_at`` returns a non-zero exit.
    """

    def __init__(self, *, fail_at: str | None = None, block: asyncio.Event | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._fail_at = fail_at
        self._block = block

    async def __call__(self, *args: str, timeout: float, **_: object) -> CapturedSubprocess:
        self.calls.append(args)
        if self._block is not None:
            await self._block.wait()
        if "venv" in args:
            Path(args[-1]).mkdir(parents=True, exist_ok=True)
        rc = 1 if self._fail_at is not None and self._fail_at in args else 0
        return CapturedSubprocess(returncode=rc, stdout=b"pretend pip output", timed_out=False)


def _patch_runner(monkeypatch: pytest.MonkeyPatch, runner: _FakeRunner) -> None:
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.env_provisioner.run_subprocess_capture",
        runner,
    )


def _venv_dir(provisioner: EnvProvisioner, version: str) -> Path:
    return provisioner.venvs_dir / f"esphome-{version}"


def _seed_venv(provisioner: EnvProvisioner, version: str) -> Path:
    """Create a ready-looking cached venv dir on disk."""
    venv = _venv_dir(provisioner, version)
    venv.mkdir(parents=True, exist_ok=True)
    (venv / ".provisioned").write_text(version)
    return venv


async def test_provision_builds_and_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """First provision runs venv + pip and marks ready; a repeat is a cache hit."""
    runner = _FakeRunner()
    _patch_runner(monkeypatch, runner)
    provisioner = EnvProvisioner(data_dir=tmp_path)

    cmd = await provisioner.provision("2026.6.4")

    assert cmd[-2:] == ["-m", "esphome"]
    assert "esphome-2026.6.4" in cmd[0]
    assert (_venv_dir(provisioner, "2026.6.4") / ".provisioned").is_file()
    assert len(runner.calls) == 2  # one venv, one pip install

    again = await provisioner.provision("2026.6.4")
    assert again == cmd
    assert len(runner.calls) == 2  # unchanged: served from cache


async def test_provision_refuses_non_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dev / prerelease target is refused before any subprocess runs."""
    runner = _FakeRunner()
    _patch_runner(monkeypatch, runner)
    provisioner = EnvProvisioner(data_dir=tmp_path)

    with pytest.raises(EnvProvisionError):
        await provisioner.provision("2026.7.0-dev")

    assert runner.calls == []


@pytest.mark.parametrize("fail_at", ["venv", "install"])
async def test_provision_failure_removes_partial_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_at: str
) -> None:
    """A failed venv or pip step raises and leaves no half-built (marked) venv."""
    runner = _FakeRunner(fail_at=fail_at)
    _patch_runner(monkeypatch, runner)
    provisioner = EnvProvisioner(data_dir=tmp_path)

    with pytest.raises(EnvProvisionError):
        await provisioner.provision("2026.6.4")

    assert not _venv_dir(provisioner, "2026.6.4").exists()


async def test_provision_concurrent_same_version_builds_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two concurrent provisions of one version share a single build (per-version lock)."""
    gate = asyncio.Event()
    runner = _FakeRunner(block=gate)
    _patch_runner(monkeypatch, runner)
    provisioner = EnvProvisioner(data_dir=tmp_path)

    first = asyncio.ensure_future(provisioner.provision("2026.6.4"))
    second = asyncio.ensure_future(provisioner.provision("2026.6.4"))
    await asyncio.sleep(0)  # let both reach the lock
    gate.set()
    cmd_a, cmd_b = await asyncio.gather(first, second)

    assert cmd_a == cmd_b
    assert len(runner.calls) == 2  # only the lock holder built; the other cache-hit


async def test_sweep_stale_removes_older_keeps_installed_and_newer(
    tmp_path: Path,
) -> None:
    """Startup sweep drops venvs older than installed; keeps equal / newer."""
    provisioner = EnvProvisioner(data_dir=tmp_path)
    older = _seed_venv(provisioner, "2026.5.0")
    same = _seed_venv(provisioner, "2026.6.4")
    newer = _seed_venv(provisioner, "2026.7.0")

    await provisioner.sweep_stale("2026.6.4")

    assert not older.exists()
    assert same.exists()
    assert newer.exists()


async def test_sweep_stale_noop_when_installed_is_dev(tmp_path: Path) -> None:
    """A dev-installed receiver can't order versions, so the sweep does nothing."""
    provisioner = EnvProvisioner(data_dir=tmp_path)
    kept = _seed_venv(provisioner, "2026.5.0")

    await provisioner.sweep_stale("2026.7.0-dev")

    assert kept.exists()


async def test_clean_all_removes_every_venv(tmp_path: Path) -> None:
    """The clean-build-env path wipes the whole venvs tree."""
    provisioner = EnvProvisioner(data_dir=tmp_path)
    _seed_venv(provisioner, "2026.5.0")
    _seed_venv(provisioner, "2026.6.4")

    await provisioner.clean_all()

    assert not provisioner.venvs_dir.exists()
