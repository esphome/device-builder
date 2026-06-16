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

import pytest
from esphome.storage_json import StorageJSON


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
    """The helper child emits the same entries as an in-process get_download_types call."""
    storage_path, _build = _make_storage(tmp_path, target_platform, *build_files)

    result = subprocess.run(  # noqa: S603 — args fully test-controlled
        [
            sys.executable,
            "-m",
            "esphome_device_builder.helper_cli",
            "download-types",
            str(storage_path),
            component,
        ],
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
