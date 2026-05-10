"""
Unit tests for :mod:`helpers.config_bundle`.

The bundle helper spawns ``esphome bundle <yaml> -o <tarball>``
as a subprocess (mirror of how the firmware controller spawns
``esphome compile`` / ``esphome upload``). Tests fake the
subprocess via :func:`monkeypatch.setattr` on
:mod:`helpers.subprocess.create_subprocess_exec` so the
helper's plumbing (temp-file lifecycle, error mapping, timeout
guard, missing-yaml pre-check) is exercised without invoking
real ESPHome.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from esphome_device_builder.helpers import config_bundle
from esphome_device_builder.helpers.config_bundle import (
    BundleBuildError,
    build_yaml_bundle,
)


class _FakeProc:
    """Stand-in for :class:`asyncio.subprocess.Process` driven by tests."""

    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: bytes = b"",
        output_bytes: bytes | None = None,
        output_path: Path | None = None,
        hang: bool = False,
    ) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._output_bytes = output_bytes
        self._output_path = output_path
        self._hang = hang

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._hang:
            await asyncio.sleep(3600)
            raise AssertionError("unreachable")
        # On success, materialise the bundle bytes at the
        # output path the caller supplied to the subprocess —
        # the real esphome CLI writes here.
        if self._output_path is not None and self._output_bytes is not None:
            self._output_path.write_bytes(self._output_bytes)
        return self._stdout, b""

    def kill(self) -> None:
        pass

    async def wait(self) -> int:
        return self.returncode


def _install_fake_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int = 0,
    stdout: bytes = b"",
    output_bytes: bytes | None = None,
    hang: bool = False,
) -> list[tuple[Any, ...]]:
    """Patch ``create_subprocess_exec`` with a fake; return captured arg tuples."""
    captured: list[tuple[Any, ...]] = []

    async def _fake(*args: Any, **_kwargs: Any) -> _FakeProc:
        captured.append(args)
        # The CLI signature is ``<esphome_cmd...> bundle <yaml>
        # -o <out>``; pull the ``-o`` arg out so the fake can
        # write to the same path the real subprocess would.
        try:
            output_path = Path(args[args.index("-o") + 1])
        except (ValueError, IndexError):
            output_path = None
        return _FakeProc(
            returncode=returncode,
            stdout=stdout,
            output_bytes=output_bytes,
            output_path=output_path,
            hang=hang,
        )

    monkeypatch.setattr(config_bundle, "create_subprocess_exec", _fake)
    return captured


@pytest.mark.asyncio
async def test_build_yaml_bundle_returns_subprocess_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: subprocess exits 0 and the temp-file bytes are returned."""
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    expected = b"GZIPPED-TAR-BYTES"
    captured = _install_fake_subprocess(monkeypatch, output_bytes=expected)

    result = await build_yaml_bundle(yaml_path)
    assert result == expected
    # CLI was invoked with the ``bundle`` subcommand + yaml + -o.
    args = captured[0]
    assert "bundle" in args
    assert str(yaml_path) in args
    assert "-o" in args


@pytest.mark.asyncio
async def test_build_yaml_bundle_missing_yaml_raises_file_not_found(
    tmp_path: Path,
) -> None:
    """A missing YAML at *yaml_path* raises :class:`FileNotFoundError` upfront."""
    with pytest.raises(FileNotFoundError):
        await build_yaml_bundle(tmp_path / "missing.yaml")


@pytest.mark.asyncio
async def test_build_yaml_bundle_subprocess_failure_raises_bundle_build_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-zero exit raises :class:`BundleBuildError` with the captured output."""
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("invalid yaml content", encoding="utf-8")
    _install_fake_subprocess(
        monkeypatch,
        returncode=1,
        stdout=b"INVALID_YAML: unexpected token\n",
    )

    with pytest.raises(BundleBuildError) as exc_info:
        await build_yaml_bundle(yaml_path)
    assert "INVALID_YAML" in exc_info.value.output


@pytest.mark.asyncio
async def test_build_yaml_bundle_cleans_temp_file_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The temp output file is unlinked even when the subprocess fails."""
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    captured = _install_fake_subprocess(monkeypatch, returncode=1, stdout=b"err")

    with pytest.raises(BundleBuildError):
        await build_yaml_bundle(yaml_path)

    output_path = Path(captured[0][captured[0].index("-o") + 1])
    assert not output_path.exists()


@pytest.mark.asyncio
async def test_build_yaml_bundle_cleans_temp_file_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The temp output file is unlinked after a successful read."""
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    captured = _install_fake_subprocess(monkeypatch, output_bytes=b"bytes")

    await build_yaml_bundle(yaml_path)
    output_path = Path(captured[0][captured[0].index("-o") + 1])
    assert not output_path.exists()


@pytest.mark.asyncio
async def test_build_yaml_bundle_timeout_raises_bundle_build_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hung subprocess past the timeout raises :class:`BundleBuildError`."""
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    _install_fake_subprocess(monkeypatch, hang=True)
    monkeypatch.setattr(config_bundle, "_BUNDLE_BUILD_TIMEOUT_SECONDS", 0.05)

    with pytest.raises(BundleBuildError, match="timed out"):
        await build_yaml_bundle(yaml_path)
