"""Tests for the device-builder-helper subprocess and the runtime invariant.

``device-builder-helper download-types`` is how the dashboard answers the
build-dir-dependent platforms (libretiny / nrf52) without importing
``esphome.components.*`` in its own process. These pin that the child's JSON
matches the in-process ``get_download_types`` it replaces, and that running the
download path never pulls those modules into the main process.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest
from esphome.storage_json import StorageJSON

from esphome_device_builder import helper_cli
from esphome_device_builder.controllers.firmware.download import _helper_cmd


def _make_storage(tmp_path: Path, target_platform: str, *build_files: str) -> tuple[Path, Path]:
    """Write a StorageJSON sidecar + build dir; return ``(storage_path, build_dir)``."""
    build_dir = tmp_path / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    for name in build_files:
        path = build_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    storage = StorageJSON(
        storage_version=1,
        name="demo",
        friendly_name=None,
        comment=None,
        esphome_version=None,
        src_version=None,
        address="demo.local",
        web_port=None,
        target_platform=target_platform,
        build_path=str(build_dir),
        firmware_bin_path=str(build_dir / "firmware.bin"),
        loaded_integrations=[],
        loaded_platforms=[],
        no_mdns=False,
    )
    storage_path = tmp_path / "demo.json"
    storage.save(storage_path)
    return storage_path, build_dir


@pytest.mark.parametrize(
    ("target_platform", "component", "build_files"),
    [
        ("bk72xx", "libretiny", ("firmware.uf2",)),
        ("nrf52", "nrf52", ("zephyr/zephyr.uf2", "firmware.zip")),
    ],
)
def test_helper_download_types_matches_in_process(
    tmp_path: Path, target_platform: str, component: str, build_files: tuple[str, ...]
) -> None:
    """The helper child emits the same entries as an in-process get_download_types call.

    Runs the production command (``_helper_cmd()``) end to end, so the installed
    ``device-builder-helper`` console-script entry point is exercised under CI
    (and the ``-m`` fallback in an editable dev checkout).
    """
    storage_path, _build = _make_storage(tmp_path, target_platform, *build_files)

    result = subprocess.run(  # noqa: S603 — args fully test-controlled
        [*_helper_cmd(), "download-types", str(storage_path), component],
        check=True,
        capture_output=True,
        text=True,
    )
    child = json.loads(result.stdout)

    module = importlib.import_module(f"esphome.components.{component}")
    expected = [
        {
            "title": entry.get("title", ""),
            "description": entry.get("description", ""),
            "file": entry["file"],
        }
        for entry in module.get_download_types(StorageJSON.load(storage_path))
    ]
    assert child == expected
    assert child, "fixture should produce at least one downloadable entry"


def test_cmd_download_types_prints_entries(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """In-process: the subcommand prints the platform's download-type JSON."""
    storage_path, _build = _make_storage(tmp_path, "bk72xx", "firmware.uf2")
    args = SimpleNamespace(storage_path=str(storage_path), component="libretiny")

    assert helper_cli._cmd_download_types(args) == 0  # type: ignore[arg-type]

    entries = json.loads(capsys.readouterr().out)
    assert entries and entries[0]["file"] == "firmware.uf2"


def test_cmd_download_types_missing_storage_prints_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A missing sidecar prints ``[]`` rather than raising."""
    args = SimpleNamespace(storage_path=str(tmp_path / "absent.json"), component="libretiny")

    assert helper_cli._cmd_download_types(args) == 0  # type: ignore[arg-type]

    assert json.loads(capsys.readouterr().out) == []


def test_main_dispatches_download_types(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``main`` parses argv and dispatches the download-types subcommand."""
    storage_path, _build = _make_storage(tmp_path, "ESP8266", "firmware.bin")
    monkeypatch.setattr(
        sys, "argv", ["device-builder-helper", "download-types", str(storage_path), "esp8266"]
    )

    assert helper_cli.main() == 0

    assert any(entry["file"] == "firmware.bin" for entry in json.loads(capsys.readouterr().out))


def test_download_path_does_not_import_esphome_components(tmp_path: Path) -> None:
    """Resolving downloads for esp32 + libretiny leaves the main process esphome-free.

    esp32 is answered from the precomputed index; libretiny goes through the
    helper child. Neither should land ``esphome.components.{esp32,libretiny}`` in
    the calling process's ``sys.modules``.
    """
    script = textwrap.dedent(
        """
        import sys, tempfile
        from pathlib import Path
        from esphome.storage_json import StorageJSON
        from esphome_device_builder.controllers.firmware.download import collect_download_entries

        tmp = Path(tempfile.mkdtemp())

        def storage(target, *files):
            build = tmp / target / "build"
            build.mkdir(parents=True)
            for f in files:
                (build / f).write_bytes(b"x")
            sj = StorageJSON(
                storage_version=1, name="demo", friendly_name=None, comment=None,
                esphome_version=None, src_version=None, address="demo.local", web_port=None,
                target_platform=target, build_path=str(build),
                firmware_bin_path=build / "firmware.bin",
                loaded_integrations=[], loaded_platforms=[], no_mdns=False,
            )
            p = tmp / f"{target}.json"
            sj.save(p)
            return sj, p

        esp32_sj, esp32_p = storage("ESP32", "firmware.factory.bin")
        lt_sj, lt_p = storage("bk72xx", "firmware.uf2")
        collect_download_entries(esp32_sj, esp32_p)
        collect_download_entries(lt_sj, lt_p)

        bad = [m for m in sys.modules if m == "esphome.components.esp32"
               or m.startswith("esphome.components.esp32.")
               or m == "esphome.components.libretiny"
               or m.startswith("esphome.components.libretiny.")]
        assert not bad, bad
        """
    )
    result = subprocess.run(  # noqa: S603 — args fully test-controlled
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
