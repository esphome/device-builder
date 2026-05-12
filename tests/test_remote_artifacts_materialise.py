"""
Tests for the offloader-side materialiser.

:func:`materialise_remote_artifacts` reads the receiver's
tarball — produced by
:func:`controllers.remote_build.artifacts_tarball.pack_build_artifacts` —
and stages the build tree + sidecars at the offloader's
canonical paths so ``esphome upload`` resolves cleanly.

These tests build real tarballs through the production packer
(rather than synthetic tarballs) so the wire-format contract
between the two functions is exercised end-to-end.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest
from esphome.core import CORE

from esphome_device_builder.controllers.remote_build.artifacts_tarball import (
    IDEDATA_MEMBER_NAME,
    STORAGE_MEMBER_NAME,
    pack_build_artifacts,
)
from esphome_device_builder.helpers.remote_artifacts_materialise import (
    MaterialiseError,
    materialise_remote_artifacts,
)
from esphome_device_builder.helpers.storage_path import (
    resolve_idedata_path,
    resolve_storage_path,
)
from tests.test_remote_build_artifacts_download import _write_receiver_state


def _pack_in_tmp(
    receiver_root: Path,
    *,
    configuration: str = "kitchen.yaml",
    **kwargs: object,
) -> bytes:
    """Build a receiver-side state under *receiver_root* and pack it.

    Pins ``CORE.config_path`` to *receiver_root*'s sentinel so the
    packer's path helpers resolve into the receiver tmp tree.
    """
    sentinel = receiver_root / "___DASHBOARD_SENTINEL___.yaml"
    with patch.object(CORE, "config_path", sentinel):
        _write_receiver_state(receiver_root, configuration=configuration, **kwargs)  # type: ignore[arg-type]
        packed = pack_build_artifacts(configuration)
    return packed.tarball


def _materialise_in_tmp(
    tarball: bytes,
    offloader_root: Path,
    *,
    configuration: str = "kitchen.yaml",
) -> Path:
    """Materialise *tarball* into *offloader_root*'s .esphome subtree.

    Pins ``CORE.config_path`` to *offloader_root*'s sentinel so
    the materialiser's path helpers resolve into the offloader
    tmp tree.
    """
    sentinel = offloader_root / "___DASHBOARD_SENTINEL___.yaml"
    with patch.object(CORE, "config_path", sentinel):
        return materialise_remote_artifacts(tarball, configuration)


# ---------------------------------------------------------------------------
# Happy path: pack → materialise round-trip
# ---------------------------------------------------------------------------


def test_materialise_stages_build_tree_and_sidecars(tmp_path: Path) -> None:
    """Build tree, storage sidecar, and idedata cache all land at the offloader's paths."""
    receiver_root = tmp_path / "receiver"
    receiver_root.mkdir()
    offloader_root = tmp_path / "offloader"
    offloader_root.mkdir()

    tarball = _pack_in_tmp(
        receiver_root,
        extras=[("bootloader.bin", "0x1000")],
        extra_build_files={
            ".pioenvs/kitchen/bootloader.bin": b"BOOT",
            ".pioenvs/kitchen/firmware.elf": b"ELF",
        },
    )
    build_path = _materialise_in_tmp(tarball, offloader_root)

    assert build_path == offloader_root / ".esphome" / "build" / "kitchen"
    assert (build_path / "platformio.ini").is_file()
    assert (build_path / ".pioenvs" / "kitchen" / "firmware.bin").is_file()
    assert (build_path / ".pioenvs" / "kitchen" / "bootloader.bin").is_file()
    assert (build_path / ".pioenvs" / "kitchen" / "firmware.elf").is_file()
    # Metadata members do NOT extract into the build tree —
    # they go to the offloader's cache locations.
    assert not (build_path / STORAGE_MEMBER_NAME).exists()
    assert not (build_path / IDEDATA_MEMBER_NAME).exists()


def test_materialise_storage_sidecar_carries_receiver_metadata(tmp_path: Path) -> None:
    """Receiver's target_platform / framework / name flow through unchanged."""
    receiver_root = tmp_path / "receiver"
    receiver_root.mkdir()
    offloader_root = tmp_path / "offloader"
    offloader_root.mkdir()

    tarball = _pack_in_tmp(receiver_root, target_platform="ESP32")

    sentinel = offloader_root / "___DASHBOARD_SENTINEL___.yaml"
    with patch.object(CORE, "config_path", sentinel):
        materialise_remote_artifacts(tarball, "kitchen.yaml")
        storage_path = resolve_storage_path("kitchen.yaml")
    data = json.loads(storage_path.read_text())

    # Receiver's metadata flows through unchanged.
    assert data["esp_platform"] == "ESP32"
    assert data["framework"] == "arduino"
    assert data["name"] == "kitchen"
    # build_path + firmware_bin_path are remapped to the offloader's tree.
    offloader_build_path = offloader_root / ".esphome" / "build" / "kitchen"
    assert data["build_path"] == str(offloader_build_path)
    assert data["firmware_bin_path"] == str(
        offloader_build_path / ".pioenvs" / "kitchen" / "firmware.bin"
    )


def test_materialise_libretiny_storage_preserves_uf2_basename(tmp_path: Path) -> None:
    """A libretiny build's firmware_bin_path round-trips as firmware.uf2, not firmware.bin.

    The receiver-side StorageJSON carries the platform-correct
    basename (esphome/core/__init__.py:778 returns ``firmware.uf2``
    for libretiny); the materialiser remaps only the build-dir
    prefix, leaving the basename untouched. Pins that the
    materialiser doesn't accidentally hardcode firmware.bin.
    """
    receiver_root = tmp_path / "receiver"
    receiver_root.mkdir()
    offloader_root = tmp_path / "offloader"
    offloader_root.mkdir()

    sentinel = receiver_root / "___DASHBOARD_SENTINEL___.yaml"
    with patch.object(CORE, "config_path", sentinel):
        # Manually craft a receiver state where firmware_bin_path
        # points at firmware.uf2 (mimicking libretiny's
        # CORE.firmware_bin output).
        _write_receiver_state(
            receiver_root,
            device_name="bw15",
            target_platform="BK72XX",
            extra_build_files={".pioenvs/bw15/firmware.uf2": b"UF2"},
        )
        # Override the storage sidecar's firmware_bin_path to .uf2.
        storage_path = resolve_storage_path("kitchen.yaml")
        data = json.loads(storage_path.read_text())
        data["firmware_bin_path"] = str(
            receiver_root / ".esphome" / "build" / "bw15" / ".pioenvs" / "bw15" / "firmware.uf2"
        )
        storage_path.write_text(json.dumps(data) + "\n")
        packed = pack_build_artifacts("kitchen.yaml")

    _materialise_in_tmp(packed.tarball, offloader_root)

    sentinel = offloader_root / "___DASHBOARD_SENTINEL___.yaml"
    with patch.object(CORE, "config_path", sentinel):
        offloader_storage_path = resolve_storage_path("kitchen.yaml")
    data = json.loads(offloader_storage_path.read_text())
    assert data["firmware_bin_path"].endswith("/.pioenvs/bw15/firmware.uf2"), (
        f"libretiny .uf2 should survive the round-trip, got {data['firmware_bin_path']!r}"
    )


def test_materialise_idedata_remaps_prog_path_and_flash_images(tmp_path: Path) -> None:
    """Idedata's prog_path + extra.flash_images[*].path all remap to the offloader tree."""
    receiver_root = tmp_path / "receiver"
    receiver_root.mkdir()
    offloader_root = tmp_path / "offloader"
    offloader_root.mkdir()

    tarball = _pack_in_tmp(
        receiver_root,
        extras=[("bootloader.bin", "0x1000"), ("partitions.bin", "0x8000")],
        extra_build_files={
            ".pioenvs/kitchen/bootloader.bin": b"BOOT",
            ".pioenvs/kitchen/partitions.bin": b"PART",
        },
    )
    _materialise_in_tmp(tarball, offloader_root)

    sentinel = offloader_root / "___DASHBOARD_SENTINEL___.yaml"
    with patch.object(CORE, "config_path", sentinel):
        cached = resolve_idedata_path("kitchen.yaml", name="kitchen")
    data = json.loads(cached.read_text())

    offloader_build_path = offloader_root / ".esphome" / "build" / "kitchen"
    pioenvs = offloader_build_path / ".pioenvs" / "kitchen"
    assert data["prog_path"] == str(pioenvs / "firmware.elf")
    paths = [entry["path"] for entry in data["extra"]["flash_images"]]
    assert paths == [
        str(pioenvs / "bootloader.bin"),
        str(pioenvs / "partitions.bin"),
    ]


def test_materialise_idedata_remaps_cc_path_to_offloader_pio_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cc_path's PIO core prefix swaps to the offloader's PLATFORMIO_CORE_DIR."""
    receiver_root = tmp_path / "receiver"
    receiver_root.mkdir()
    offloader_root = tmp_path / "offloader"
    offloader_root.mkdir()
    offloader_pio = tmp_path / "offloader_pio"
    monkeypatch.setenv("PLATFORMIO_CORE_DIR", str(offloader_pio))

    tarball = _pack_in_tmp(receiver_root)
    _materialise_in_tmp(tarball, offloader_root)

    sentinel = offloader_root / "___DASHBOARD_SENTINEL___.yaml"
    with patch.object(CORE, "config_path", sentinel):
        cached = resolve_idedata_path("kitchen.yaml", name="kitchen")
    data = json.loads(cached.read_text())

    # The receiver's cc_path was
    #   /home/receiver/.platformio/packages/toolchain-xtensa32/bin/xtensa-esp32-elf-gcc
    # The materialiser keys off "packages/" and prepends the
    # offloader's PIO core dir.
    assert data["cc_path"] == str(
        offloader_pio / "packages" / "toolchain-xtensa32" / "bin" / "xtensa-esp32-elf-gcc"
    )


def test_materialise_idedata_drops_unparseable_cc_path(tmp_path: Path) -> None:
    """cc_path without a 'packages/' segment is dropped from the staged idedata."""
    receiver_root = tmp_path / "receiver"
    receiver_root.mkdir()
    offloader_root = tmp_path / "offloader"
    offloader_root.mkdir()

    sentinel = receiver_root / "___DASHBOARD_SENTINEL___.yaml"
    with patch.object(CORE, "config_path", sentinel):
        _write_receiver_state(receiver_root)
        idedata_path = resolve_idedata_path("kitchen.yaml", name="kitchen")
        data = json.loads(idedata_path.read_text())
        data["cc_path"] = "/usr/bin/gcc"  # no packages/ segment
        idedata_path.write_text(json.dumps(data) + "\n")
        packed = pack_build_artifacts("kitchen.yaml")

    _materialise_in_tmp(packed.tarball, offloader_root)

    sentinel = offloader_root / "___DASHBOARD_SENTINEL___.yaml"
    with patch.object(CORE, "config_path", sentinel):
        cached = resolve_idedata_path("kitchen.yaml", name="kitchen")
    data = json.loads(cached.read_text())
    assert "cc_path" not in data


def test_materialise_touches_mtimes_for_esphome_cache_hit(tmp_path: Path) -> None:
    """platformio.ini.mtime ends up strictly older than the staged idedata's mtime.

    Pins the contract that ``_load_idedata`` reads — the cache
    check is ``platformio_ini.mtime >= cached.mtime``, so we
    need the cached file to be newer to skip regeneration.
    """
    receiver_root = tmp_path / "receiver"
    receiver_root.mkdir()
    offloader_root = tmp_path / "offloader"
    offloader_root.mkdir()

    tarball = _pack_in_tmp(receiver_root)
    build_path = _materialise_in_tmp(tarball, offloader_root)

    platformio_ini = build_path / "platformio.ini"
    sentinel = offloader_root / "___DASHBOARD_SENTINEL___.yaml"
    with patch.object(CORE, "config_path", sentinel):
        cached = resolve_idedata_path("kitchen.yaml", name="kitchen")
    assert platformio_ini.stat().st_mtime < cached.stat().st_mtime


def test_materialise_idempotent_under_rerun(tmp_path: Path) -> None:
    """Re-running materialise over an existing staged tree overwrites cleanly."""
    receiver_root = tmp_path / "receiver"
    receiver_root.mkdir()
    offloader_root = tmp_path / "offloader"
    offloader_root.mkdir()

    tarball = _pack_in_tmp(receiver_root)
    first = _materialise_in_tmp(tarball, offloader_root)
    # Plant a stale file in the build tree before re-materialising
    # to confirm extraction overwrites cleanly.
    stale = first / ".pioenvs" / "kitchen" / "stale.bin"
    stale.write_bytes(b"STALE")
    second = _materialise_in_tmp(tarball, offloader_root)

    assert first == second
    # The materialised firmware.bin re-exists. (We don't assert on
    # the stale file's removal — extract over existing files just
    # rewrites the named members; leftover files aren't actively
    # cleaned.)
    assert (second / ".pioenvs" / "kitchen" / "firmware.bin").is_file()


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------

# Placeholder for a "build_path" field in synthetic tarballs that
# the materialiser rejects before extraction.
_FAKE_BUILD_PATH = "/fake/receiver/build/path"


def test_materialise_rejects_missing_storage_member(tmp_path: Path) -> None:
    """A tarball without storage.json raises MaterialiseError with a clear message."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name=IDEDATA_MEMBER_NAME)
        info.size = 2
        tar.addfile(info, io.BytesIO(b"{}"))

    with pytest.raises(MaterialiseError, match=r"missing required member: 'storage\.json'"):
        _materialise_in_tmp(buf.getvalue(), tmp_path)


def test_materialise_rejects_missing_idedata_member(tmp_path: Path) -> None:
    """A tarball without idedata.json raises MaterialiseError."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        storage = json.dumps(
            {"storage_version": 1, "name": "kitchen", "build_path": _FAKE_BUILD_PATH}
        ).encode("utf-8")
        info = tarfile.TarInfo(name=STORAGE_MEMBER_NAME)
        info.size = len(storage)
        tar.addfile(info, io.BytesIO(storage))

    with pytest.raises(MaterialiseError, match=r"missing required member: 'idedata\.json'"):
        _materialise_in_tmp(buf.getvalue(), tmp_path)


def test_materialise_rejects_path_traversal(tmp_path: Path) -> None:
    """Members that resolve outside the build dir raise before extraction."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        storage = json.dumps(
            {"storage_version": 1, "name": "kitchen", "build_path": _FAKE_BUILD_PATH}
        ).encode("utf-8")
        info = tarfile.TarInfo(name=STORAGE_MEMBER_NAME)
        info.size = len(storage)
        tar.addfile(info, io.BytesIO(storage))

        idedata = b"{}"
        info = tarfile.TarInfo(name=IDEDATA_MEMBER_NAME)
        info.size = len(idedata)
        tar.addfile(info, io.BytesIO(idedata))

        # Member with a traversal-shaped path.
        evil_payload = b"EVIL"
        info = tarfile.TarInfo(name="../../../etc/passwd")
        info.size = len(evil_payload)
        tar.addfile(info, io.BytesIO(evil_payload))

    with pytest.raises(MaterialiseError, match=r"escapes destination"):
        _materialise_in_tmp(buf.getvalue(), tmp_path)


def test_materialise_rejects_storage_missing_name(tmp_path: Path) -> None:
    """storage.json without a 'name' field raises before extraction starts."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        storage = json.dumps({"storage_version": 1, "build_path": _FAKE_BUILD_PATH}).encode("utf-8")
        info = tarfile.TarInfo(name=STORAGE_MEMBER_NAME)
        info.size = len(storage)
        tar.addfile(info, io.BytesIO(storage))
        idedata = b"{}"
        info = tarfile.TarInfo(name=IDEDATA_MEMBER_NAME)
        info.size = len(idedata)
        tar.addfile(info, io.BytesIO(idedata))

    with pytest.raises(MaterialiseError, match=r"missing required name field"):
        _materialise_in_tmp(buf.getvalue(), tmp_path)


def test_materialise_rejects_storage_missing_build_path(tmp_path: Path) -> None:
    """storage.json without a 'build_path' field raises."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        storage = json.dumps({"storage_version": 1, "name": "kitchen"}).encode("utf-8")
        info = tarfile.TarInfo(name=STORAGE_MEMBER_NAME)
        info.size = len(storage)
        tar.addfile(info, io.BytesIO(storage))
        idedata = b"{}"
        info = tarfile.TarInfo(name=IDEDATA_MEMBER_NAME)
        info.size = len(idedata)
        tar.addfile(info, io.BytesIO(idedata))

    with pytest.raises(MaterialiseError, match=r"missing required build_path field"):
        _materialise_in_tmp(buf.getvalue(), tmp_path)


def test_materialise_rejects_malformed_tarball(tmp_path: Path) -> None:
    """Random bytes that aren't a gzipped tar surface as MaterialiseError."""
    with pytest.raises(MaterialiseError, match=r"malformed"):
        _materialise_in_tmp(b"definitely not a tarball", tmp_path)


def test_materialise_rejects_non_json_storage(tmp_path: Path) -> None:
    """storage.json that isn't parseable JSON raises MaterialiseError."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name=STORAGE_MEMBER_NAME)
        info.size = 4
        tar.addfile(info, io.BytesIO(b"{bad"))
        info = tarfile.TarInfo(name=IDEDATA_MEMBER_NAME)
        info.size = 2
        tar.addfile(info, io.BytesIO(b"{}"))

    with pytest.raises(MaterialiseError, match=r"not valid JSON"):
        _materialise_in_tmp(buf.getvalue(), tmp_path)


def test_materialise_rejects_non_dict_idedata(tmp_path: Path) -> None:
    """idedata.json that parses to a non-dict raises MaterialiseError."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        storage = json.dumps(
            {"storage_version": 1, "name": "kitchen", "build_path": _FAKE_BUILD_PATH}
        ).encode("utf-8")
        info = tarfile.TarInfo(name=STORAGE_MEMBER_NAME)
        info.size = len(storage)
        tar.addfile(info, io.BytesIO(storage))
        # idedata parses but isn't a dict.
        info = tarfile.TarInfo(name=IDEDATA_MEMBER_NAME)
        info.size = 4
        tar.addfile(info, io.BytesIO(b"null"))

    with pytest.raises(MaterialiseError, match=r"is not a JSON object"):
        _materialise_in_tmp(buf.getvalue(), tmp_path)
