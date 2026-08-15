"""
Unit tests for :mod:`helpers.config_bundle`.

The bundle helper spawns ``esphome bundle <yaml> -o <tarball>`` and
streams its output. Tests monkeypatch ``find_esphome_cmd`` to a tiny
``sys.executable`` script standing in for esphome, so the helper's
plumbing (streaming, temp-file lifecycle, error mapping, timeout,
missing-yaml pre-check) is exercised against a real subprocess.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from esphome_device_builder.helpers import config_bundle
from esphome_device_builder.helpers.config_bundle import (
    BundleBuildError,
    build_yaml_bundle,
)

_SCRIPT_PRELUDE = "import sys, time\nout = sys.argv[sys.argv.index('-o') + 1]\n"


def _install_fake_esphome(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    body: str,
    *,
    patch_output_path: bool = True,
) -> Path:
    """Point ``find_esphome_cmd`` at a stand-in script; return the reserved output path."""
    script = tmp_path / "fake_esphome.py"
    script.write_text(_SCRIPT_PRELUDE + body, encoding="utf-8")
    monkeypatch.setattr(config_bundle, "find_esphome_cmd", lambda: [sys.executable, str(script)])
    output_path = tmp_path / "bundle-out.tar.gz"
    if patch_output_path:
        monkeypatch.setattr(config_bundle, "_allocate_temp_bundle_path", lambda: output_path)
    return output_path


def _write_yaml(tmp_path: Path) -> Path:
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    return yaml_path


async def test_build_yaml_bundle_returns_subprocess_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: subprocess exits 0 and the temp-file bytes are returned."""
    yaml_path = _write_yaml(tmp_path)
    _install_fake_esphome(
        monkeypatch,
        tmp_path,
        "assert sys.argv[1] == 'bundle'\n"
        f"assert sys.argv[2] == {str(yaml_path)!r}\n"
        "open(out, 'wb').write(b'GZIPPED-TAR-BYTES')\n",
        patch_output_path=False,
    )

    assert await build_yaml_bundle(yaml_path) == b"GZIPPED-TAR-BYTES"


async def test_build_yaml_bundle_streams_output_to_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``on_output`` receives each subprocess chunk with its terminator."""
    yaml_path = _write_yaml(tmp_path)
    _install_fake_esphome(
        monkeypatch,
        tmp_path,
        "print('INFO Reading configuration...')\n"
        "print('INFO Bundling 3 files')\n"
        "open(out, 'wb').write(b'bytes')\n",
    )

    chunks: list[str] = []
    await build_yaml_bundle(yaml_path, on_output=chunks.append)
    # Windows children emit \r\n; the terminator is preserved either way.
    assert all(chunk.endswith("\n") for chunk in chunks)
    stripped = [chunk.rstrip("\r\n") for chunk in chunks]
    assert "INFO Reading configuration..." in stripped
    assert "INFO Bundling 3 files" in stripped


async def test_build_yaml_bundle_missing_yaml_raises_file_not_found(
    tmp_path: Path,
) -> None:
    """A missing YAML at *yaml_path* raises :class:`FileNotFoundError` upfront."""
    with pytest.raises(FileNotFoundError):
        await build_yaml_bundle(tmp_path / "missing.yaml")


async def test_build_yaml_bundle_subprocess_failure_raises_bundle_build_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-zero exit raises :class:`BundleBuildError` carrying the captured output."""
    yaml_path = _write_yaml(tmp_path)
    _install_fake_esphome(
        monkeypatch,
        tmp_path,
        "print('INVALID_YAML: unexpected token')\nsys.exit(1)\n",
    )

    with pytest.raises(BundleBuildError, match="exited 1") as exc_info:
        await build_yaml_bundle(yaml_path)
    assert "INVALID_YAML" in exc_info.value.output


async def test_build_yaml_bundle_captures_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stderr merges into the streamed output so validator errors surface."""
    yaml_path = _write_yaml(tmp_path)
    _install_fake_esphome(
        monkeypatch,
        tmp_path,
        "print('ERROR bad platform', file=sys.stderr)\nsys.exit(2)\n",
    )

    with pytest.raises(BundleBuildError) as exc_info:
        await build_yaml_bundle(yaml_path)
    assert "ERROR bad platform" in exc_info.value.output


async def test_build_yaml_bundle_cleans_temp_file_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The temp output file is unlinked even when the subprocess fails."""
    yaml_path = _write_yaml(tmp_path)
    output_path = _install_fake_esphome(
        monkeypatch,
        tmp_path,
        "open(out, 'wb').write(b'partial')\nsys.exit(1)\n",
    )

    with pytest.raises(BundleBuildError):
        await build_yaml_bundle(yaml_path)
    assert not output_path.exists()


async def test_build_yaml_bundle_cleans_temp_file_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The temp output file is unlinked after a successful read."""
    yaml_path = _write_yaml(tmp_path)
    output_path = _install_fake_esphome(monkeypatch, tmp_path, "open(out, 'wb').write(b'bytes')\n")

    await build_yaml_bundle(yaml_path)
    assert not output_path.exists()


async def test_build_yaml_bundle_cancel_kills_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling the build kills the bundle subprocess and propagates the cancel."""
    yaml_path = _write_yaml(tmp_path)
    _install_fake_esphome(
        monkeypatch,
        tmp_path,
        "print('started', flush=True)\ntime.sleep(30)\n",
    )

    chunks: list[str] = []
    task = asyncio.get_running_loop().create_task(
        build_yaml_bundle(yaml_path, on_output=chunks.append)
    )
    while not chunks:
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    # Let the child watcher reap the SIGKILL'd process before the
    # test loop closes, or its transport __del__ warns at teardown.
    await asyncio.sleep(0.1)


async def test_build_yaml_bundle_timeout_raises_bundle_build_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timed-out subprocess raises with the pre-timeout output preserved."""
    yaml_path = _write_yaml(tmp_path)
    _install_fake_esphome(
        monkeypatch,
        tmp_path,
        "print('EARLY DIAGNOSTIC', flush=True)\ntime.sleep(30)\n",
    )
    # The timeout clock starts after spawn but still covers the child's Python
    # interpreter cold-start + first print; on a loaded CI runner that can take
    # most of a second, so keep comfortable margin or the timeout fires with an
    # empty buffer and the pre-timeout-output assertion below flakes.
    monkeypatch.setattr(config_bundle, "_BUNDLE_BUILD_TIMEOUT_SECONDS", 5.0)

    with pytest.raises(BundleBuildError, match="timed out") as exc_info:
        await build_yaml_bundle(yaml_path)
    assert "EARLY DIAGNOSTIC" in exc_info.value.output
