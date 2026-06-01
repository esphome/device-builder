"""
Registry-level coverage for the per-platform artifact module pack.

Each ``controllers.remote_build.artifact_platforms.*`` module
declares one platform's build-tree inclusion list; this test
walks the registry without invoking the packer to assert the
shape invariants the packer relies on.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest

from esphome_device_builder.controllers.remote_build import artifact_platforms
from esphome_device_builder.controllers.remote_build.artifact_platforms import (
    _libretiny,
    bk72xx,
    build_files_for_platform,
    esp32,
    ln882x,
    rtl87xx,
)


def _public_platform_modules() -> list:
    """Yield every non-underscored module in the ``artifact_platforms`` package.

    The registry's ``_PLATFORMS`` tuple is the canonical
    enumeration, but walking the package directly lets the test
    also catch a new module that's been added without a
    corresponding import in ``__init__.py``.
    """
    modules = []
    for module_info in pkgutil.iter_modules(artifact_platforms.__path__):
        if module_info.name.startswith("_"):
            continue
        modules.append(
            importlib.import_module(
                f"esphome_device_builder.controllers.remote_build.artifact_platforms."
                f"{module_info.name}"
            )
        )
    return modules


def test_every_module_exposes_target_platform_and_build_files() -> None:
    modules = _public_platform_modules()
    assert modules, "expected at least one platform module"
    for module in modules:
        assert isinstance(getattr(module, "TARGET_PLATFORM", None), str), (
            f"{module.__name__} missing TARGET_PLATFORM"
        )
        build_files = getattr(module, "BUILD_FILES", None)
        assert isinstance(build_files, tuple) and build_files, (
            f"{module.__name__} missing or empty BUILD_FILES"
        )
        for entry in build_files:
            assert isinstance(entry, str), (
                f"{module.__name__} BUILD_FILES carries non-str: {entry!r}"
            )


def test_target_platform_values_are_unique() -> None:
    seen: dict[str, str] = {}
    for module in _public_platform_modules():
        key = module.TARGET_PLATFORM.lower()
        prior = seen.get(key)
        assert prior is None, f"duplicate TARGET_PLATFORM {key!r}: {prior} and {module.__name__}"
        seen[key] = module.__name__


def test_build_files_entries_are_build_relative_paths() -> None:
    """Every BUILD_FILES entry is relative (joined against ``<build_path>/``)."""
    for module in _public_platform_modules():
        for entry in module.BUILD_FILES:
            assert not entry.startswith("/"), (
                f"{module.__name__} BUILD_FILES carries an absolute path: {entry!r}"
            )
            assert entry.format(name="x"), (
                f"{module.__name__} BUILD_FILES entry {entry!r} didn't format"
            )


def test_libretiny_family_shares_build_files() -> None:
    """bk72xx / rtl87xx / ln882x re-export the shared libretiny tuple."""
    shared = _libretiny.BUILD_FILES
    assert bk72xx.BUILD_FILES is shared
    assert rtl87xx.BUILD_FILES is shared
    assert ln882x.BUILD_FILES is shared


@pytest.mark.parametrize("lookup", ["esp32", "ESP32", "Esp32"])
def test_lookup_is_case_insensitive_for_canonical_values(lookup: str) -> None:
    """Upstream serialises ``target_platform.upper()`` so the registry must accept that."""
    assert build_files_for_platform(lookup), f"expected non-empty BUILD_FILES for {lookup!r}"


def test_lookup_returns_empty_for_unknown_platform() -> None:
    assert build_files_for_platform("nonexistent_platform") == ()


@pytest.mark.parametrize("variant", ["ESP32S3", "ESP32C3", "ESP32H2", "ESP32S2", "ESP32C6"])
def test_esp32_chip_variants_fold_to_esp32_module(variant: str) -> None:
    """Every ESP32 chip variant resolves to the esp32 BUILD_FILES tuple."""
    assert build_files_for_platform(variant) is esp32.BUILD_FILES


def test_esp32_includes_multi_image_bootloader_set() -> None:
    """ESP32 wired flash needs bootloader + partitions + ota_data + firmware."""
    rendered = [f.format(name="kitchen") for f in esp32.BUILD_FILES]
    assert ".pioenvs/kitchen/firmware.bin" in rendered
    assert ".pioenvs/kitchen/bootloader.bin" in rendered
    assert ".pioenvs/kitchen/partitions.bin" in rendered
    assert ".pioenvs/kitchen/ota_data_initial.bin" in rendered


def test_esp32_includes_factory_firmware_for_idf() -> None:
    """ESP32 BUILD_FILES carries the factory firmware (download-factory path)."""
    rendered = [f.format(name="kitchen") for f in esp32.BUILD_FILES]
    assert ".pioenvs/kitchen/firmware.factory.bin" in rendered
    assert "build/firmware.factory.bin" in rendered


def test_esp32_includes_native_idf_elf() -> None:
    """Native-IDF emits build/firmware.elf; BUILD_FILES ships it for the ELF download."""
    rendered = [f.format(name="kitchen") for f in esp32.BUILD_FILES]
    assert ".pioenvs/kitchen/firmware.elf" in rendered
    assert "build/firmware.elf" in rendered


def test_libretiny_includes_uf2_and_bin() -> None:
    """Libretiny ships both .uf2 (UART/ltchiptool) and .bin (OTA)."""
    rendered = [f.format(name="bw15") for f in build_files_for_platform("bk72xx")]
    assert ".pioenvs/bw15/firmware.uf2" in rendered
    assert ".pioenvs/bw15/firmware.bin" in rendered


def test_nrf52_includes_zephyr_app_update() -> None:
    """nrf52 component reads app_update.bin from .pioenvs/<name>/zephyr/."""
    rendered = [f.format(name="locker") for f in build_files_for_platform("nrf52")]
    assert ".pioenvs/locker/zephyr/app_update.bin" in rendered
