"""
Unit tests for :mod:`helpers.config_bundle`.

The bundle helper spawns ``esphome bundle <yaml> -o <tarball>``
through :func:`helpers.subprocess.run_subprocess_capture`. Tests
monkeypatch :func:`run_subprocess_capture` on the
:mod:`config_bundle` namespace with a fake that materialises the
expected bundle bytes at the output path, so the helper's
plumbing (temp-file lifecycle, error mapping, missing-yaml
pre-check) is exercised without invoking real ESPHome.
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
from esphome_device_builder.helpers.subprocess import CapturedSubprocess


def _install_fake_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int = 0,
    stdout: bytes = b"",
    output_bytes: bytes | None = None,
    timed_out: bool = False,
) -> list[tuple[Any, ...]]:
    """Patch ``run_subprocess_capture`` with a fake; return captured arg tuples.

    On a non-timed-out success-path call, the fake materialises
    *output_bytes* at the output path the helper passed via
    ``-o`` so the read-back step finds real bytes.
    """
    captured: list[tuple[Any, ...]] = []

    async def _fake(*args: Any, **_kwargs: Any) -> CapturedSubprocess:
        captured.append(args)
        if not timed_out and output_bytes is not None and "-o" in args:
            try:
                output_path = Path(args[args.index("-o") + 1])
            except IndexError:  # pragma: no cover — defensive
                output_path = None
            else:
                # ``write_bytes`` is a blocking syscall; the
                # production helper materialises bytes via the
                # real esphome subprocess (no event-loop block),
                # so the fake has to mirror that by deferring
                # the write to a worker thread. blockbuster on
                # Linux CI catches direct ``write_bytes`` on the
                # loop.
                await asyncio.to_thread(output_path.write_bytes, output_bytes)
        return CapturedSubprocess(returncode=returncode, stdout=stdout, timed_out=timed_out)

    monkeypatch.setattr(config_bundle, "run_subprocess_capture", _fake)
    return captured


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


async def test_build_yaml_bundle_missing_yaml_raises_file_not_found(
    tmp_path: Path,
) -> None:
    """A missing YAML at *yaml_path* raises :class:`FileNotFoundError` upfront."""
    with pytest.raises(FileNotFoundError):
        await build_yaml_bundle(tmp_path / "missing.yaml")


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


async def test_build_yaml_bundle_timeout_raises_bundle_build_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timed-out subprocess raises :class:`BundleBuildError` with 'timed out'."""
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    _install_fake_subprocess(monkeypatch, timed_out=True)

    with pytest.raises(BundleBuildError, match="timed out"):
        await build_yaml_bundle(yaml_path)
