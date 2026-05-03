"""Shared fixtures for ``tests/controllers/devices/``.

The seed-a-real-device-on-disk shape (YAML + StorageJSON sidecar +
``.device-builder.json`` entry) was duplicated across ``test_archive.py``
and ``test_rename_inline_e2e.py`` and is the natural home for the
next handful of e2e tests that walk the file-ops layer. Centralising
keeps the YAML / StorageJSON shape one place to audit when ESPHome
adds fields to the upstream ``StorageJSON`` schema.

The sync ``set_device_metadata`` write goes through
``metadata_transaction`` which calls ``tempfile.mkstemp`` — that
trips ``blockbuster`` from an async test context. The fixture
wraps it in ``asyncio.to_thread`` so callers don't have to remember.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Protocol

import pytest

from esphome_device_builder.controllers.config import set_device_metadata


class SeedDeviceFactory(Protocol):
    """Shape of the ``seed_device`` fixture's return value."""

    async def __call__(
        self,
        config_dir: Path,
        configuration: str,
        *,
        friendly_name: str | None = ...,
        storage_friendly: str | None = ...,
        board_id: str = ...,
        loaded_integrations: list[str] | None = ...,
        with_build_dir: bool = ...,
        address: str | None = ...,
        write_metadata: bool = ...,
    ) -> tuple[Path, Path]: ...


@pytest.fixture
def seed_device() -> SeedDeviceFactory:
    """Return a coroutine that seeds a fully-populated device on disk.

    Drops three artefacts mirroring what a real ``devices/create``
    + first compile would produce:

    - ``<config_dir>/<configuration>`` — a minimal but valid
      ESPHome YAML with ``esphome.name`` matching the filename
      stem and a configurable ``friendly_name``.
    - ``<config_dir>/.esphome/storage/<configuration>.json`` —
      a full ``StorageJSON`` sidecar shaped like what ``esphome
      compile --only-generate`` writes (every field the dashboard
      reads from is present).
    - ``<config_dir>/.device-builder.json`` — a sidecar metadata
      entry with ``board_id`` + ``friendly_name`` so tests that
      exercise the metadata-move branch (rename) or the
      identity-keep branch (archive) have something to assert.

    ``storage_friendly`` lets a caller pin the StorageJSON's
    ``friendly_name`` to a value distinct from the YAML stem so
    rename tests can verify the "friendly_name == old_name →
    rewrite" conditional branches the helpers under test.

    Async because the sidecar write goes through
    ``metadata_transaction`` (which calls ``tempfile.mkstemp``);
    blockbuster on the CI Linux runners flags the underlying
    ``os.path.abspath`` from a synchronous async-test context.
    Pushing the write to a thread keeps the seed call cheap to
    use from any ``@pytest.mark.asyncio`` test.
    """

    async def _seed(
        config_dir: Path,
        configuration: str,
        *,
        friendly_name: str | None = None,
        storage_friendly: str | None = None,
        board_id: str = "generic-esp32c3",
        loaded_integrations: list[str] | None = None,
        with_build_dir: bool = False,
        address: str | None = None,
        write_metadata: bool = True,
    ) -> tuple[Path, Path]:
        name = configuration.removesuffix(".yaml").removesuffix(".yml")
        yaml_path = config_dir / configuration
        yaml_text = (
            "esphome:\n"
            f"  name: {name}\n"
            f'  friendly_name: "{friendly_name or name.title()}"\n'
            "  platform: ESP32\n"
            "  board: esp32-c3-devkitm-1\n"
        )
        yaml_path.write_text(yaml_text, encoding="utf-8")

        # Build dir is opt-in — only delete / archive flows actually
        # care about its presence. Default off keeps the seed call
        # cheap for tests that just need YAML + storage + sidecar.
        build_path = config_dir / ".esphome" / "build" / name
        if with_build_dir:
            build_path.mkdir(parents=True, exist_ok=True)
            (build_path / "firmware.bin").write_bytes(b"\x00" * 16)
            (build_path / "src").mkdir()
            (build_path / "src" / "main.cpp").write_text("// fake\n", encoding="utf-8")

        storage_dir = config_dir / ".esphome" / "storage"
        storage_dir.mkdir(parents=True, exist_ok=True)
        (storage_dir / f"{configuration}.json").write_text(
            json.dumps(
                {
                    "storage_version": 1,
                    "name": name,
                    "friendly_name": (storage_friendly if storage_friendly is not None else name),
                    "comment": None,
                    "esphome_version": "2026.5.0-dev",
                    "src_version": 1,
                    "address": address if address is not None else f"{name}.local",
                    "web_port": None,
                    "esp_platform": "esp32",
                    "board": "esp32-c3-devkitm-1",
                    "build_path": str(build_path),
                    "firmware_bin_path": str(build_path / ".pioenvs" / "firmware.bin"),
                    "loaded_integrations": loaded_integrations
                    if loaded_integrations is not None
                    else ["api"],
                    "loaded_platforms": ["esp32"],
                    "no_mdns": False,
                    "framework": "esp-idf",
                    "core_platform": "esp32",
                    "target_platform": "esp32",
                }
            ),
            encoding="utf-8",
        )

        # ``metadata_transaction`` reaches into ``tempfile.mkstemp``
        # which is sync; push it to a thread so blockbuster doesn't
        # fault on the ``os.path.abspath`` from inside an async test.
        # Skip when the caller wants a bare seed (``write_metadata=False``)
        # — useful for tests that exercise "no identity fields ever
        # set" branches and need an empty ``.device-builder.json``.
        if write_metadata:
            await asyncio.to_thread(
                set_device_metadata,
                config_dir,
                configuration,
                board_id=board_id,
                friendly_name=friendly_name or name,
            )
        return yaml_path, build_path

    return _seed


@pytest.fixture
def redirect_storage_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point ``ext_storage_path`` at ``tmp_path/.esphome/storage/``.

    The real helper walks ``CORE.config_path`` which isn't set in
    isolated tests; redirecting keeps the file ops on disk under
    the test's tmp dir without spinning up a CORE. Used by tests
    that exercise the StorageJSON-move helpers (rename, archive).
    """
    storage_dir = tmp_path / ".esphome" / "storage"
    storage_dir.mkdir(parents=True, exist_ok=True)

    def _ext(configuration: str) -> Path:
        return storage_dir / f"{configuration}.json"

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.controller.ext_storage_path",
        _ext,
    )
    # ``_archive_single`` lives in ``controller.py`` but delegates to
    # ``_wipe_device_build_dir`` and ``_remove_device_sidecars`` over
    # in ``helpers.py``. Both files import ``ext_storage_path``
    # independently; rebinding only one leaves the other path running
    # against the real CORE.
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.helpers.ext_storage_path",
        _ext,
    )

    # ``StorageJSON.save`` itself uses the upstream ``ext_storage_path``
    # if a caller passes the configuration name without a path; for
    # safety, also patch that lookup so any indirect caller stays
    # under ``tmp_path``.
    monkeypatch.setattr(
        "esphome.storage_json.ext_storage_path",
        _ext,
        raising=False,
    )
