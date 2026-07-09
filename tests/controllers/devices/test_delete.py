"""
Tests for ``DevicesController._delete_single``.

The legacy dashboard wiped ``<config_dir>/.esphome/build/<name>/``
on archive (``shutil.rmtree(storage_json.build_path, ...)``); the
new backend skipped it, so repeated create-delete cycles leaked
hundreds of MB of PlatformIO state per device and a recycled name
picked up stale build artefacts on the next compile. The fix
reads ``StorageJSON.build_path`` and ``rmtree``s it before the
YAML is unlinked, so a partial failure leaves the user able to
retry.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode
from tests._storage_fixtures import write_storage_json

from .conftest import MakeControllerFactory


def _cache_paths(config_dir: Path, configuration: str) -> tuple[Path, Path]:
    """Return ``(idedata_cache, validated_config_cache)`` for *configuration*.

    The idedata cache is keyed on the device ``name`` (the YAML
    stem here); the validated-config cache is keyed on the YAML
    filename — matching esphome's own layout under ``.esphome``.
    """
    stem = Path(configuration).stem
    idedata = config_dir / ".esphome" / "idedata" / f"{stem}.json"
    validated = config_dir / ".esphome" / "storage" / f"{configuration}.validated.yaml"
    return idedata, validated


def _seed_device(
    config_dir: Path, configuration: str, *, with_build_dir: bool = True
) -> tuple[Path, Path]:
    """Lay out a YAML, StorageJSON sidecar, build tree, and build caches.

    Returns ``(yaml_path, build_path)`` so the test can assert the
    rmtree happened on the right directory. The idedata + validated-
    config caches are seeded too (see :func:`_cache_paths`).
    """
    yaml_path = config_dir / configuration
    yaml_path.write_text(f"esphome:\n  name: {Path(configuration).stem}\n", encoding="utf-8")

    build_path = config_dir / ".esphome" / "build" / Path(configuration).stem
    if with_build_dir:
        build_path.mkdir(parents=True, exist_ok=True)
        # Drop a couple of files so the rmtree has something to remove —
        # an empty directory would still be unlinked but wouldn't catch
        # the bug where the rmtree never runs at all.
        (build_path / "firmware.bin").write_bytes(b"\x00" * 16)
        (build_path / "src").mkdir()
        (build_path / "src" / "main.cpp").write_text("// fake\n", encoding="utf-8")

    idedata, validated = _cache_paths(config_dir, configuration)
    idedata.parent.mkdir(parents=True, exist_ok=True)
    idedata.write_text("{}", encoding="utf-8")
    validated.parent.mkdir(parents=True, exist_ok=True)
    validated.write_text("esphome: {}\n", encoding="utf-8")

    write_storage_json(
        config_dir,
        configuration,
        firmware_bin_path=build_path / ".pioenvs" / "firmware.bin",
        build_path=build_path,
    )
    return yaml_path, build_path


@pytest.mark.usefixtures("redirect_storage_path")
async def test_delete_wipes_build_directory(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """The PlatformIO build tree goes away with the device.

    Without this, a recycled device name picks up stale ``.pioenvs``
    state on the next compile and we leak disk on every churn.
    """
    controller = make_controller(tmp_path)
    yaml_path, build_path = _seed_device(tmp_path, "kitchen.yaml")
    idedata, validated = _cache_paths(tmp_path, "kitchen.yaml")

    await controller._delete_single("kitchen.yaml")

    assert not yaml_path.exists()
    assert not build_path.exists()
    assert not (tmp_path / ".esphome" / "storage" / "kitchen.yaml.json").exists()
    # Regenerable build caches keyed off the device go too, so a
    # recycled name doesn't pick up stale idedata / validated YAML.
    assert not idedata.exists()
    assert not validated.exists()


@pytest.mark.usefixtures("redirect_storage_path")
async def test_delete_wipes_idedata_when_no_build_path(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A never-built device (build_path None) still has its idedata cache wiped."""
    controller = make_controller(tmp_path)
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    idedata, _ = _cache_paths(tmp_path, "kitchen.yaml")
    idedata.parent.mkdir(parents=True, exist_ok=True)
    idedata.write_text("{}", encoding="utf-8")
    write_storage_json(tmp_path, "kitchen.yaml", overrides={"build_path": None})

    await controller._delete_single("kitchen.yaml")

    assert not yaml_path.exists()
    assert not idedata.exists()


@pytest.mark.usefixtures("redirect_storage_path")
async def test_delete_tolerates_idedata_unlink_failure(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """An OSError unlinking the idedata cache doesn't unwind the delete."""
    controller = make_controller(tmp_path)
    yaml_path, _ = _seed_device(tmp_path, "kitchen.yaml")
    idedata, _ = _cache_paths(tmp_path, "kitchen.yaml")
    # Swap the cache file for a directory so unlink() raises OSError.
    idedata.unlink()
    idedata.mkdir()

    await controller._delete_single("kitchen.yaml")

    assert not yaml_path.exists()


@pytest.mark.usefixtures("redirect_storage_path")
async def test_delete_tolerates_compiled_config_unlink_failure(
    tmp_path: Path, make_controller: MakeControllerFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """An OSError unlinking the validated-config cache is logged, not raised."""
    controller = make_controller(tmp_path)
    yaml_path, _ = _seed_device(tmp_path, "kitchen.yaml")
    _, validated = _cache_paths(tmp_path, "kitchen.yaml")
    validated.unlink()
    validated.mkdir()

    with caplog.at_level(logging.DEBUG):
        await controller._delete_single("kitchen.yaml")

    assert not yaml_path.exists()
    assert any("unlink_compiled_config" in rec.message for rec in caplog.records)


@pytest.mark.usefixtures("redirect_storage_path")
async def test_delete_succeeds_when_never_compiled(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A device that's never been built has no sidecar — delete must still succeed."""
    controller = make_controller(tmp_path)
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n", encoding="utf-8")

    await controller._delete_single("kitchen.yaml")

    assert not yaml_path.exists()


@pytest.mark.usefixtures("redirect_storage_path")
async def test_delete_tolerates_missing_build_directory(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Sidecar present but build tree already wiped — delete must not raise."""
    controller = make_controller(tmp_path)
    yaml_path, build_path = _seed_device(tmp_path, "kitchen.yaml", with_build_dir=False)
    assert not build_path.exists()

    await controller._delete_single("kitchen.yaml")

    assert not yaml_path.exists()


@pytest.mark.usefixtures("redirect_storage_path")
async def test_delete_tolerates_rmtree_failure(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Windows-style rmtree failure on the build dir doesn't unwind the delete."""
    controller = make_controller(tmp_path)
    yaml_path, build_path = _seed_device(tmp_path, "kitchen.yaml")

    def _flaky(path: object, *args: object, **kwargs: object) -> None:
        raise PermissionError("simulated read-only file on windows")

    monkeypatch.setattr("esphome_device_builder.helpers.build_artifacts.rmtree", _flaky)

    await controller._delete_single("kitchen.yaml")

    # YAML + sidecar still gone; build dir survives because rmtree raised.
    assert not yaml_path.exists()
    assert not (tmp_path / ".esphome" / "storage" / "kitchen.yaml.json").exists()
    assert build_path.exists()


async def test_delete_raises_when_yaml_missing(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Missing YAML pre-check still fires before any cleanup runs."""
    controller = make_controller(tmp_path)

    with pytest.raises(CommandError) as exc_info:
        await controller._delete_single("ghost.yaml")
    assert exc_info.value.code == ErrorCode.NOT_FOUND


async def test_delete_device_missing_surfaces_not_found(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """The WS-layer entry point surfaces ``CommandError(NOT_FOUND)``, not internal_error."""
    controller = make_controller(tmp_path)

    with pytest.raises(CommandError) as exc_info:
        await controller.delete_device(configuration="ghost.yaml")
    assert exc_info.value.code == ErrorCode.NOT_FOUND
