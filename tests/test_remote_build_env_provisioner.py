"""Coverage for the receiver-side esphome venv provisioner engine."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from esphome_device_builder.controllers.remote_build.env_provisioner import (
    EnvProvisioner,
    EnvProvisionError,
    _venv_python,
)
from esphome_device_builder.helpers.async_ import run_in_executor
from esphome_device_builder.helpers.subprocess import CapturedSubprocess


def _make_venv_python(venv: Path) -> None:
    """Create the venv's python file, as ``python -m venv`` would."""
    python = _venv_python(venv)
    python.parent.mkdir(parents=True, exist_ok=True)
    python.touch()


class _FakeRunner:
    """Stand-in for ``run_subprocess_capture`` that records calls.

    On the ``venv`` command it creates the venv's python file, so the health
    probe's ``is_file`` fast-path passes and its ``esphome version`` call runs
    (mirroring what ``python -m venv`` + install would leave behind). The
    ``version`` probe reports the version parsed from the venv dir, or
    ``reports_version`` when set (to simulate a drifted cache). A command whose
    args contain ``fail_at`` returns a non-zero exit.
    """

    def __init__(
        self,
        *,
        fail_at: str | None = None,
        block: asyncio.Event | None = None,
        reports_version: str | None = None,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._fail_at = fail_at
        self._block = block
        self._reports_version = reports_version

    async def __call__(self, *args: str, timeout: float, **_: object) -> CapturedSubprocess:
        self.calls.append(args)
        if self._block is not None:
            await self._block.wait()
        if "venv" in args:
            await run_in_executor(_make_venv_python, Path(args[-1]))
        if self._fail_at is not None and self._fail_at in args:
            return CapturedSubprocess(returncode=1, stdout=b"pretend output", timed_out=False)
        stdout = b"pretend output"
        if "version" in args:  # health probe: echo the venv's esphome version
            reported = self._reports_version
            if reported is None:
                reported = Path(args[0]).parent.parent.name.removeprefix("esphome-")
            stdout = f"Version: {reported}\n".encode()
        return CapturedSubprocess(returncode=0, stdout=stdout, timed_out=False)

    def count(self, token: str) -> int:
        return sum(token in call for call in self.calls)


def _patch_runner(monkeypatch: pytest.MonkeyPatch, runner: _FakeRunner) -> None:
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.env_provisioner.run_subprocess_capture",
        runner,
    )


def _venv_dir(provisioner: EnvProvisioner, version: str) -> Path:
    return provisioner.venvs_dir / f"esphome-{version}"


async def _seed_venv(provisioner: EnvProvisioner, version: str) -> Path:
    """Create a cached venv dir on disk (recognised by the sweep / clean)."""
    venv = _venv_dir(provisioner, version)
    await run_in_executor(_mkdirs, venv)
    return venv


def _mkdirs(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


async def test_provision_builds_and_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """First provision runs venv + pip + health probe; a repeat is a cache hit."""
    runner = _FakeRunner()
    _patch_runner(monkeypatch, runner)
    provisioner = EnvProvisioner(data_dir=tmp_path)

    cmd = await provisioner.provision("2026.6.4")

    assert cmd[-2:] == ["-m", "esphome"]
    assert "esphome-2026.6.4" in cmd[0]
    assert (runner.count("venv"), runner.count("install"), runner.count("version")) == (1, 1, 1)

    again = await provisioner.provision("2026.6.4")
    assert again == cmd
    # No rebuild (venv / install unchanged); the health probe runs each time.
    assert (runner.count("venv"), runner.count("install")) == (1, 1)
    assert runner.count("version") == 2


async def test_provision_rebuilds_unhealthy_existing_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leftover venv dir that fails the health check is rebuilt, not reused."""
    runner = _FakeRunner()
    _patch_runner(monkeypatch, runner)
    provisioner = EnvProvisioner(data_dir=tmp_path)
    # A crash-interrupted build leaves the dir present but with no runnable
    # esphome, so the health probe's is_file fast-path fails.
    await _seed_venv(provisioner, "2026.6.4")

    cmd = await provisioner.provision("2026.6.4")

    assert "esphome-2026.6.4" in cmd[0]
    assert (runner.count("venv"), runner.count("install")) == (1, 1)  # rebuilt


async def test_provision_rejects_venv_reporting_wrong_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A venv that runs but reports a different version fails the health check."""
    runner = _FakeRunner(reports_version="2025.1.0")  # never the requested version
    _patch_runner(monkeypatch, runner)
    provisioner = EnvProvisioner(data_dir=tmp_path)

    with pytest.raises(EnvProvisionError, match="health check"):
        await provisioner.provision("2026.6.4")

    assert not _venv_dir(provisioner, "2026.6.4").exists()


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


@pytest.mark.parametrize("fail_at", ["venv", "install", "version"])
async def test_provision_failure_removes_partial_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_at: str
) -> None:
    """A failed venv / pip / health step raises and leaves no usable venv behind."""
    runner = _FakeRunner(fail_at=fail_at)
    _patch_runner(monkeypatch, runner)
    provisioner = EnvProvisioner(data_dir=tmp_path)

    with pytest.raises(EnvProvisionError):
        await provisioner.provision("2026.6.4")

    assert not _venv_dir(provisioner, "2026.6.4").exists()


async def test_provision_timeout_reports_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A step that times out raises with a ``timed out`` status, not empty output."""

    async def _timeout(*_args: str, timeout: float, **_: object) -> CapturedSubprocess:
        return CapturedSubprocess(returncode=None, stdout=b"", timed_out=True)

    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.env_provisioner.run_subprocess_capture",
        _timeout,
    )
    provisioner = EnvProvisioner(data_dir=tmp_path)

    with pytest.raises(EnvProvisionError, match="timed out"):
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

    first = asyncio.create_task(provisioner.provision("2026.6.4"))
    second = asyncio.create_task(provisioner.provision("2026.6.4"))
    await asyncio.sleep(0)  # let both reach the lock
    gate.set()
    cmd_a, cmd_b = await asyncio.gather(first, second)

    assert cmd_a == cmd_b
    assert runner.count("venv") == 1  # only the lock holder built; the other cache-hit


async def test_sweep_stale_removes_older_keeps_installed_and_newer(tmp_path: Path) -> None:
    """Startup sweep drops venvs older than installed; keeps equal / newer."""
    provisioner = EnvProvisioner(data_dir=tmp_path)
    older = await _seed_venv(provisioner, "2026.5.0")
    same = await _seed_venv(provisioner, "2026.6.4")
    newer = await _seed_venv(provisioner, "2026.7.0")

    await provisioner.sweep_stale("2026.6.4")

    assert not older.exists()
    assert same.exists()
    assert newer.exists()


async def test_sweep_stale_tolerates_missing_dir_and_skips_non_venv_entries(
    tmp_path: Path,
) -> None:
    """Sweep is a no-op with no venvs dir and leaves non-``esphome-`` entries alone."""
    provisioner = EnvProvisioner(data_dir=tmp_path)
    # Nothing provisioned yet: the venvs dir doesn't exist.
    await provisioner.sweep_stale("2026.6.4")

    older = await _seed_venv(provisioner, "2026.5.0")
    stray = provisioner.venvs_dir / "not-a-venv"
    await run_in_executor(_mkdirs, stray)

    await provisioner.sweep_stale("2026.6.4")

    assert not older.exists()  # older release swept
    assert stray.exists()  # unrelated entry untouched


async def test_sweep_stale_noop_when_installed_is_dev(tmp_path: Path) -> None:
    """A dev-installed receiver can't order versions, so the sweep does nothing."""
    provisioner = EnvProvisioner(data_dir=tmp_path)
    kept = await _seed_venv(provisioner, "2026.5.0")

    await provisioner.sweep_stale("2026.7.0-dev")

    assert kept.exists()


async def test_clean_all_removes_every_venv(tmp_path: Path) -> None:
    """The clean-build-env path wipes the whole venvs tree."""
    provisioner = EnvProvisioner(data_dir=tmp_path)
    await _seed_venv(provisioner, "2026.5.0")
    await _seed_venv(provisioner, "2026.6.4")

    await provisioner.clean_all()

    assert not provisioner.venvs_dir.exists()
