"""End-to-end coverage for ``FirmwareController.get_binaries``.

The handler reads ``StorageJSON`` for *configuration* and resolves the
downloadable artifacts for its ``target_platform``. Since the dashboard process
must not import ``esphome.components.*``, the source of the platform's download
types is split:

- Static platforms (esp32 / esp8266 / rp2040) come from the generated
  ``platform_capabilities.index.json``.
- Build-dir-dependent platforms (libretiny / nrf52) are answered by the
  ``device-builder-helper`` subprocess.

This file pins the resolution split (``_download_types_for``), the
``_resolve_download_component`` table, and the transforms layered on the
resolved list (filter to files on disk, append ``firmware.elf``, tag types).
"""

from __future__ import annotations

import logging
import types
from pathlib import Path
from typing import Any

import pytest

from esphome_device_builder.controllers.firmware import download as download_mod
from esphome_device_builder.controllers.firmware.download import (
    _download_types_for,
    _platform_sets,
    _resolve_download_component,
)
from tests._storage_fixtures import write_storage_json
from tests.controllers.firmware.conftest import FirmwareControllerFactory

# Variant / family sets the resolver routes, read from the same generated index
# production uses. Driven off the index (not a live esphome import) so the test
# is independent of the installed esphome version (CI runs stable / beta / dev).
_ESP32_VARIANTS = sorted(_platform_sets().esp32_variants)
_LIBRETINY_TARGET_PLATFORMS = _platform_sets().libretiny_targets


@pytest.fixture(autouse=True)
def _redirect_ext_storage_path(monkeypatch: Any, tmp_path: Path) -> None:
    """Pin ``resolve_storage_path`` at ``<tmp>/.esphome/storage/<config>.json``.

    ``CORE.config_path`` isn't initialised in the test process, so the
    controller-side binding gets the tmpfs layout instead.
    """
    monkeypatch.setattr(
        "esphome_device_builder.controllers.firmware.download.resolve_storage_path",
        lambda configuration: tmp_path / ".esphome" / "storage" / f"{configuration}.json",
    )


def _stub_download_types(monkeypatch: Any, types_returned: list[dict]) -> list[Any]:
    """Stub the ``_download_types_for`` seam, returning the captured-storage list.

    Replaces the platform-source resolution (index / helper subprocess) so the
    transform tests pin filter / elf / tag behaviour independent of where the
    raw list came from.
    """
    captured: list[Any] = []

    def _fake(storage: Any, storage_path: Any = None, *, label: Any = None) -> list[dict]:
        captured.append(storage)
        return [dict(entry) for entry in types_returned]

    monkeypatch.setattr(download_mod, "_download_types_for", _fake)
    return captured


# The ELF entry ``get_binaries`` appends when ``firmware.elf`` is on disk.
_ELF_ENTRY = {
    "title": "ELF (for debugging)",
    "description": "Debug symbols for the ESP stack trace decoder.",
    "file": "firmware.elf",
    "type": "elf",
}


def _make_build(tmp_path: Path, *files: str) -> Path:
    """Create a fake ``.pioenvs/kitchen`` build dir holding *files*.

    Returns the directory so the test can point ``firmware_bin_path``
    at a sibling -- ``get_binaries`` only stats the entry files and
    ``firmware.elf`` under this parent, so ``firmware.bin`` itself need
    not exist.
    """
    build_dir = tmp_path / ".esphome" / "build" / "kitchen" / ".pioenvs" / "kitchen"
    build_dir.mkdir(parents=True, exist_ok=True)
    for name in files:
        (build_dir / name).write_bytes(b"x")
    return build_dir


# ---------------------------------------------------------------------------
# _resolve_download_component
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("variant", sorted(_ESP32_VARIANTS))
def test_resolve_download_component_routes_every_esp32_variant_to_umbrella(
    variant: str,
) -> None:
    """Every ESP32 variant in the index maps to the umbrella ``"esp32"`` component.

    Driven off the generated index (what routing actually reads), not a live
    esphome import, so it's independent of the installed esphome version. Both
    the canonical upper-case form and a lower-case round-trip are checked since
    ``StorageJSON`` sometimes stores the lower-cased value.
    """
    assert _resolve_download_component(variant) == "esp32"
    assert _resolve_download_component(variant.lower()) == "esp32"


@pytest.mark.parametrize("family", sorted(_LIBRETINY_TARGET_PLATFORMS))
def test_resolve_download_component_routes_every_libretiny_family_to_umbrella(
    family: str,
) -> None:
    """Every LibreTiny family in ``_LIBRETINY_TARGET_PLATFORMS`` routes to ``"libretiny"``."""
    assert _resolve_download_component(family) == "libretiny"


@pytest.mark.parametrize("platform", ["rp2040", "host", "rtl8710b-unknown-vendor"])
def test_resolve_download_component_passes_unmapped_platforms_through(
    platform: str,
) -> None:
    """Non-mapped platforms pass through verbatim."""
    assert _resolve_download_component(platform) == platform


def test_resolve_download_component_handles_none() -> None:
    """Nullable ``StorageJSON.target_platform`` flows through without an explicit coerce."""
    assert _resolve_download_component(None) == ""
    assert _resolve_download_component("") == ""


def test_resolve_download_component_folds_esp32_variants_when_index_degraded(
    monkeypatch: Any,
) -> None:
    """An empty (degraded) index still folds esp32 variants to the umbrella component.

    A missing index then makes an ESP32 variant download slow (helper spawn) rather
    than broken (helper importing a nonexistent ``esphome.components.esp32s3``).
    """
    empty = download_mod._DownloadRouting(frozenset(), frozenset())
    monkeypatch.setattr(download_mod, "_platform_sets", lambda: empty)
    assert _resolve_download_component("ESP32S3") == "esp32"
    assert _resolve_download_component("esp32c3") == "esp32"
    assert _resolve_download_component("esp32") == "esp32"
    assert _resolve_download_component("esp8266") == "esp8266"  # non-esp32 unaffected


# ---------------------------------------------------------------------------
# _download_types_for — source resolution (index vs helper)
# ---------------------------------------------------------------------------


def test_download_types_for_static_platform_uses_index(monkeypatch: Any) -> None:
    """An esp32 device reads the precomputed index, never spawning the helper."""

    def _no_helper(*_args: Any, **_kwargs: Any) -> list[dict]:
        raise AssertionError("helper must not run for a precomputed platform")

    monkeypatch.setattr(download_mod.subprocess, "run", _no_helper)
    storage = types.SimpleNamespace(target_platform="ESP32C3", name="kitchen")

    result = _download_types_for(storage, None, label="kitchen")

    files = {entry["file"] for entry in result}
    assert files == {"firmware.factory.bin", "firmware.ota.bin"}


def test_download_types_for_dynamic_platform_calls_helper(monkeypatch: Any) -> None:
    """A libretiny device routes through the helper subprocess and parses its JSON."""
    seen_cmd: list[str] = []

    def _fake_run(cmd: list[str], **_kwargs: Any) -> Any:
        seen_cmd.extend(cmd)
        return types.SimpleNamespace(
            returncode=0, stdout='[{"title": "UF2", "description": "", "file": "firmware.uf2"}]'
        )

    monkeypatch.setattr(download_mod.subprocess, "run", _fake_run)
    storage = types.SimpleNamespace(target_platform="bk72xx", name="kitchen")

    result = _download_types_for(storage, Path("kitchen.json"), label="kitchen")

    assert result == [{"title": "UF2", "description": "", "file": "firmware.uf2"}]
    assert "download-types" in seen_cmd
    assert "libretiny" in seen_cmd


def test_download_types_for_dynamic_platform_without_path_is_empty(monkeypatch: Any) -> None:
    """A dynamic platform with no storage path can't spawn the helper -> empty."""

    def _no_helper(*_args: Any, **_kwargs: Any) -> list[dict]:
        raise AssertionError("helper must not run without a storage path")

    monkeypatch.setattr(download_mod.subprocess, "run", _no_helper)
    storage = types.SimpleNamespace(target_platform="bk72xx", name="kitchen")

    assert _download_types_for(storage, None, label="kitchen") == []


def test_download_types_for_coerces_malformed_helper_reply(monkeypatch: Any) -> None:
    """A malformed helper reply is coerced; only well-shaped entries survive."""

    def _fake_run(cmd: list[str], **_kwargs: Any) -> Any:
        return types.SimpleNamespace(
            returncode=0,
            stdout='[{"file": "firmware.uf2"}, {"title": "no file"}, "garbage", 7]',
        )

    monkeypatch.setattr(download_mod.subprocess, "run", _fake_run)
    storage = types.SimpleNamespace(target_platform="bk72xx", name="kitchen")

    result = _download_types_for(storage, Path("kitchen.json"), label="kitchen")

    assert result == [{"title": "", "description": "", "file": "firmware.uf2"}]


def test_download_types_for_non_list_helper_reply_is_empty(monkeypatch: Any) -> None:
    """A helper reply that isn't a JSON array degrades to empty, never raising."""

    def _fake_run(cmd: list[str], **_kwargs: Any) -> Any:
        return types.SimpleNamespace(returncode=0, stdout='{"not": "a list"}')

    monkeypatch.setattr(download_mod.subprocess, "run", _fake_run)
    storage = types.SimpleNamespace(target_platform="bk72xx", name="kitchen")

    assert _download_types_for(storage, Path("kitchen.json"), label="kitchen") == []


def test_download_types_for_invalid_json_helper_reply_is_empty(
    monkeypatch: Any, caplog: Any
) -> None:
    """Unparsable helper stdout degrades to empty with a diagnosable warning."""

    def _fake_run(cmd: list[str], **_kwargs: Any) -> Any:
        return types.SimpleNamespace(returncode=0, stdout="not json{", stderr="")

    monkeypatch.setattr(download_mod.subprocess, "run", _fake_run)
    storage = types.SimpleNamespace(target_platform="bk72xx", name="kitchen")

    with caplog.at_level(logging.WARNING):
        result = _download_types_for(storage, Path("kitchen.json"), label="kitchen")

    assert result == []
    assert any("returned non-JSON for kitchen" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# get_binaries — failure / fallback branches
# ---------------------------------------------------------------------------


async def test_get_binaries_returns_empty_when_storage_missing(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """No StorageJSON sidecar -> empty list, NOT a raise.

    The "Web Serial install" picker calls ``get_binaries`` for every listed
    device on render; raising for never-compiled devices would torpedo the
    whole listing, so ``[]`` lets the picker show "compile first" inline.
    """
    controller = firmware_controller_factory()

    result = await controller.get_binaries(configuration="kitchen.yaml")

    assert result == []


async def test_get_binaries_returns_empty_when_target_platform_missing(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """Sidecar exists but ``target_platform`` is empty -> empty list (no helper spawn)."""
    write_storage_json(tmp_path, "kitchen.yaml", overrides={"esp_platform": ""})
    controller = firmware_controller_factory()

    result = await controller.get_binaries(configuration="kitchen.yaml")

    assert result == []


async def test_get_binaries_logs_and_returns_empty_on_helper_failure(
    tmp_path: Path,
    caplog: Any,
    monkeypatch: Any,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A failing helper subprocess -> empty list + warning.

    Defense-in-depth: an esphome regression that breaks ``get_download_types``
    for a dynamic platform shouldn't take down the listing for unrelated
    devices. Pin the warning so the regression is visible in the dashboard log
    rather than as silent empty rows.
    """

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("helper spawn failed")

    monkeypatch.setattr(download_mod.subprocess, "run", _boom)

    build_dir = _make_build(tmp_path, "firmware.uf2")
    write_storage_json(
        tmp_path,
        "kitchen.yaml",
        firmware_bin_path=build_dir / "firmware.bin",
        overrides={"esp_platform": "bk72xx"},
    )
    controller = firmware_controller_factory()

    with caplog.at_level(logging.WARNING):
        result = await controller.get_binaries(configuration="kitchen.yaml")

    assert result == []
    assert any(
        "download-types helper failed for kitchen.yaml" in rec.message for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# get_binaries — precomputed (static) platforms, end to end (no stub)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("esp_platform", "files"),
    [
        ("esp32c3", {"firmware.factory.bin", "firmware.ota.bin"}),
        ("esp8266", {"firmware.bin"}),
        ("rp2040", {"firmware.uf2", "firmware.ota.bin"}),
        # rp2 folds to the rp2040-keyed index entries.
        ("rp2", {"firmware.uf2", "firmware.ota.bin"}),
    ],
)
async def test_get_binaries_static_platform_uses_precomputed_index(
    esp_platform: str,
    files: set[str],
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Each precomputed platform offers the index's entries that exist on disk."""
    build_dir = _make_build(tmp_path, *files)
    write_storage_json(
        tmp_path,
        "kitchen.yaml",
        firmware_bin_path=build_dir / "firmware.bin",
        overrides={"esp_platform": esp_platform},
    )
    controller = firmware_controller_factory()

    result = await controller.get_binaries(configuration="kitchen.yaml")

    assert {entry["file"] for entry in result} == files
    assert all(entry.get("type") for entry in result)


@pytest.mark.parametrize(
    ("esp_platform", "present", "expected_file"),
    [
        ("bk72xx", ["firmware.uf2"], "firmware.uf2"),
        ("nrf52", ["zephyr/zephyr.uf2", "firmware.zip"], "zephyr/zephyr.uf2"),
    ],
)
async def test_get_binaries_dynamic_platform_uses_helper(
    esp_platform: str,
    present: list[str],
    expected_file: str,
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Libretiny / nrf52 resolve through the real device-builder-helper subprocess.

    Spawns the child (full esphome import) so the on-disk entries it reports for
    a build flow through ``get_binaries`` end to end, not just the golden /
    mocked seams.
    """
    build_dir = tmp_path / ".esphome" / "build" / "kitchen" / ".pioenvs" / "kitchen"
    for name in present:
        path = build_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    write_storage_json(
        tmp_path,
        "kitchen.yaml",
        firmware_bin_path=build_dir / "firmware.bin",
        overrides={"esp_platform": esp_platform},
    )
    controller = firmware_controller_factory()

    result = await controller.get_binaries(configuration="kitchen.yaml")

    assert expected_file in {entry["file"] for entry in result}


# ---------------------------------------------------------------------------
# get_binaries — transforms layered on the resolved list
# ---------------------------------------------------------------------------


async def test_get_binaries_filters_to_files_present_on_disk(
    tmp_path: Path, monkeypatch: Any, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """Only resolved entries whose file exists are returned."""
    factory = {"title": "Modern (Web Serial)", "file": "firmware.factory.bin", "type": "factory"}
    ota = {"title": "OTA Update", "file": "firmware.ota.bin", "type": "ota"}
    boot = {"title": "Boot App 0", "file": "boot_app0.bin"}
    _stub_download_types(monkeypatch, [factory, ota, boot])

    build_dir = _make_build(tmp_path, "firmware.factory.bin", "firmware.ota.bin")
    write_storage_json(
        tmp_path,
        "kitchen.yaml",
        firmware_bin_path=build_dir / "firmware.bin",
        overrides={"esp_platform": "esp32"},
    )
    controller = firmware_controller_factory()

    result = await controller.get_binaries(configuration="kitchen.yaml")

    assert result == [factory, ota]


async def test_get_binaries_appends_elf_entry_when_present(
    tmp_path: Path, monkeypatch: Any, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """``firmware.elf`` is offered as an extra entry for the stack trace decoder."""
    factory = {"title": "Modern (Web Serial)", "file": "firmware.factory.bin", "type": "factory"}
    _stub_download_types(monkeypatch, [factory])

    build_dir = _make_build(tmp_path, "firmware.factory.bin", "firmware.elf")
    write_storage_json(
        tmp_path,
        "kitchen.yaml",
        firmware_bin_path=build_dir / "firmware.bin",
        overrides={"esp_platform": "esp32"},
    )
    controller = firmware_controller_factory()

    result = await controller.get_binaries(configuration="kitchen.yaml")

    assert result == [factory, _ELF_ENTRY]


async def test_get_binaries_does_not_duplicate_elf_listed_upstream(
    tmp_path: Path, monkeypatch: Any, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """If a platform's download types ever list firmware.elf, it appears once."""
    elf = {"title": "Upstream ELF", "file": "firmware.elf", "type": "elf"}
    _stub_download_types(monkeypatch, [elf])

    build_dir = _make_build(tmp_path, "firmware.elf")
    write_storage_json(
        tmp_path,
        "kitchen.yaml",
        firmware_bin_path=build_dir / "firmware.bin",
        overrides={"esp_platform": "esp32"},
    )
    controller = firmware_controller_factory()

    result = await controller.get_binaries(configuration="kitchen.yaml")

    assert result == [elf]
    assert sum(entry["file"] == "firmware.elf" for entry in result) == 1


async def test_get_binaries_omits_elf_entry_when_absent(
    tmp_path: Path, monkeypatch: Any, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """No ``firmware.elf`` on disk -> no ELF entry."""
    factory = {"title": "Modern (Web Serial)", "file": "firmware.factory.bin", "type": "factory"}
    _stub_download_types(monkeypatch, [factory])

    build_dir = _make_build(tmp_path, "firmware.factory.bin")
    write_storage_json(
        tmp_path,
        "kitchen.yaml",
        firmware_bin_path=build_dir / "firmware.bin",
        overrides={"esp_platform": "esp32"},
    )
    controller = firmware_controller_factory()

    result = await controller.get_binaries(configuration="kitchen.yaml")

    assert result == [factory]
    assert _ELF_ENTRY not in result


async def test_get_binaries_returns_empty_when_no_build_path(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """Storage exists but ``firmware_bin_path`` is unset -> empty list."""
    write_storage_json(tmp_path, "kitchen.yaml", overrides={"esp_platform": "esp32"})
    controller = firmware_controller_factory()

    result = await controller.get_binaries(configuration="kitchen.yaml")

    assert result == []
