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
from typing import Any
from unittest.mock import patch

import pytest
from esphome.core import CORE

from esphome_device_builder.controllers.remote_build.artifacts_tarball import (
    IDEDATA_MEMBER_NAME,
    PLATFORMIO_INI_MEMBER_NAME,
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

_SENTINEL = object()
# Placeholder ``build_path`` for synthetic tarballs the materialiser
# rejects before extraction.
_FAKE_BUILD_PATH = "/fake/receiver/build/path"


def _pack_in_tmp(
    receiver_root: Path,
    *,
    configuration: str = "kitchen.yaml",
    **kwargs: object,
) -> bytes:
    """Build a receiver-side state under *receiver_root* and pack it."""
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
    """Materialise *tarball* into *offloader_root*'s .esphome subtree."""
    sentinel = offloader_root / "___DASHBOARD_SENTINEL___.yaml"
    with patch.object(CORE, "config_path", sentinel):
        return materialise_remote_artifacts(tarball, configuration)


def _synthetic_tarball(
    *,
    storage: Any = _SENTINEL,
    idedata: Any = _SENTINEL,
    platformio_ini: bytes | None = b"[env:e2e]\n",
    extra_members: list[tuple[str, bytes]] | None = None,
) -> bytes:
    """Build a minimal tarball for materialiser error-path tests.

    ``storage`` / ``idedata`` accept dict (JSON-encoded), bytes
    (raw — for malformed-JSON cases), or ``None`` (omit the
    member). ``platformio_ini`` accepts bytes or ``None`` (omit).
    Default is a valid storage shape + ``{}`` idedata + a
    minimal platformio.ini stub.
    """
    if storage is _SENTINEL:
        storage = {"storage_version": 1, "name": "kitchen", "build_path": _FAKE_BUILD_PATH}
    if idedata is _SENTINEL:
        idedata = {}
    members: list[tuple[str, bytes]] = []
    for name, value in ((STORAGE_MEMBER_NAME, storage), (IDEDATA_MEMBER_NAME, idedata)):
        if value is None:
            continue
        payload = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")
        members.append((name, payload))
    if platformio_ini is not None:
        members.append((PLATFORMIO_INI_MEMBER_NAME, platformio_ini))
    members.extend(extra_members or [])
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for member_name, member_payload in members:
            info = tarfile.TarInfo(name=member_name)
            info.size = len(member_payload)
            tar.addfile(info, io.BytesIO(member_payload))
    return buf.getvalue()


@pytest.fixture
def paired_roots(tmp_path: Path) -> tuple[Path, Path]:
    """Return ``(receiver_root, offloader_root)`` directories under tmp_path."""
    receiver = tmp_path / "receiver"
    receiver.mkdir()
    offloader = tmp_path / "offloader"
    offloader.mkdir()
    return receiver, offloader


# ---------------------------------------------------------------------------
# Happy path: pack → materialise round-trip
# ---------------------------------------------------------------------------


def test_materialise_stages_build_tree_and_sidecars(
    paired_roots: tuple[Path, Path],
) -> None:
    """Build tree, storage sidecar, and idedata cache all land at the offloader's paths."""
    receiver_root, offloader_root = paired_roots
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


def test_materialise_storage_sidecar_carries_receiver_metadata(
    paired_roots: tuple[Path, Path],
) -> None:
    """Receiver's target_platform / framework / name flow through unchanged."""
    receiver_root, offloader_root = paired_roots
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


def test_materialise_libretiny_storage_preserves_uf2_basename(
    paired_roots: tuple[Path, Path],
) -> None:
    """Libretiny build's firmware_bin_path round-trips as firmware.uf2, not firmware.bin."""
    receiver_root, offloader_root = paired_roots
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
    assert Path(data["firmware_bin_path"]).parts[-3:] == (".pioenvs", "bw15", "firmware.uf2"), (
        f"libretiny .uf2 should survive the round-trip, got {data['firmware_bin_path']!r}"
    )


def test_materialise_idedata_remaps_prog_path_and_flash_images(
    paired_roots: tuple[Path, Path],
) -> None:
    """Idedata's prog_path + extra.flash_images[*].path all remap to the offloader tree."""
    receiver_root, offloader_root = paired_roots
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
    paired_roots: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cc_path's PIO core prefix swaps to the offloader's PLATFORMIO_CORE_DIR."""
    receiver_root, offloader_root = paired_roots
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


def test_materialise_idedata_drops_unparseable_cc_path(
    paired_roots: tuple[Path, Path],
) -> None:
    """cc_path without a 'packages/' segment is dropped from the staged idedata."""
    receiver_root, offloader_root = paired_roots
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


def test_materialise_touches_mtimes_for_esphome_cache_hit(
    paired_roots: tuple[Path, Path],
) -> None:
    """platformio.ini.mtime ends up strictly older than the staged idedata's mtime."""
    receiver_root, offloader_root = paired_roots
    tarball = _pack_in_tmp(receiver_root)
    build_path = _materialise_in_tmp(tarball, offloader_root)

    platformio_ini = build_path / "platformio.ini"
    sentinel = offloader_root / "___DASHBOARD_SENTINEL___.yaml"
    with patch.object(CORE, "config_path", sentinel):
        cached = resolve_idedata_path("kitchen.yaml", name="kitchen")
    assert platformio_ini.stat().st_mtime < cached.stat().st_mtime


def test_materialise_idempotent_under_rerun(paired_roots: tuple[Path, Path]) -> None:
    """Re-running materialise wipes stale files from the build dir."""
    receiver_root, offloader_root = paired_roots
    tarball = _pack_in_tmp(receiver_root)
    first = _materialise_in_tmp(tarball, offloader_root)
    # Plant a stale file the second materialise should clear.
    stale = first / ".pioenvs" / "kitchen" / "stale.bin"
    stale.write_bytes(b"STALE")

    second = _materialise_in_tmp(tarball, offloader_root)

    assert first == second
    assert (second / ".pioenvs" / "kitchen" / "firmware.bin").is_file()
    assert not stale.exists(), "stale file should be cleared by the pre-extract rmtree"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_materialise_rejects_missing_storage_member(tmp_path: Path) -> None:
    """A tarball without storage.json raises MaterialiseError with a clear message."""
    tarball = _synthetic_tarball(storage=None)
    with pytest.raises(MaterialiseError, match=r"missing required member: 'storage\.json'"):
        _materialise_in_tmp(tarball, tmp_path)


def test_materialise_rejects_missing_idedata_member(tmp_path: Path) -> None:
    """A tarball without idedata.json raises MaterialiseError."""
    tarball = _synthetic_tarball(idedata=None)
    with pytest.raises(MaterialiseError, match=r"missing required member: 'idedata\.json'"):
        _materialise_in_tmp(tarball, tmp_path)


def test_materialise_rejects_path_traversal(tmp_path: Path) -> None:
    """Members that resolve outside the build dir raise before extraction."""
    tarball = _synthetic_tarball(extra_members=[("../../../etc/passwd", b"EVIL")])
    with pytest.raises(MaterialiseError, match=r"escapes destination"):
        _materialise_in_tmp(tarball, tmp_path)


def test_materialise_rejects_traversal_in_storage_name(tmp_path: Path) -> None:
    """A storage.json ``name`` carrying path-separator chars is rejected."""
    tarball = _synthetic_tarball(
        storage={"storage_version": 1, "name": "../sneaky", "build_path": _FAKE_BUILD_PATH},
    )
    with pytest.raises(MaterialiseError, match=r"not safe for a path segment"):
        _materialise_in_tmp(tarball, tmp_path)


def test_materialise_rejects_storage_missing_name(tmp_path: Path) -> None:
    """storage.json without a 'name' field raises before extraction starts."""
    tarball = _synthetic_tarball(
        storage={"storage_version": 1, "build_path": _FAKE_BUILD_PATH},
    )
    with pytest.raises(MaterialiseError, match=r"missing required name field"):
        _materialise_in_tmp(tarball, tmp_path)


def test_materialise_rejects_storage_missing_build_path(tmp_path: Path) -> None:
    """storage.json without a 'build_path' field raises."""
    tarball = _synthetic_tarball(storage={"storage_version": 1, "name": "kitchen"})
    with pytest.raises(MaterialiseError, match=r"missing required build_path field"):
        _materialise_in_tmp(tarball, tmp_path)


def test_materialise_rejects_malformed_tarball(tmp_path: Path) -> None:
    """Random bytes that aren't a gzipped tar surface as MaterialiseError."""
    with pytest.raises(MaterialiseError, match=r"malformed"):
        _materialise_in_tmp(b"definitely not a tarball", tmp_path)


def test_materialise_rejects_non_json_storage(tmp_path: Path) -> None:
    """storage.json that isn't parseable JSON raises MaterialiseError."""
    tarball = _synthetic_tarball(storage=b"{bad")
    with pytest.raises(MaterialiseError, match=r"not valid JSON"):
        _materialise_in_tmp(tarball, tmp_path)


def test_materialise_rejects_missing_platformio_ini(tmp_path: Path) -> None:
    """A tarball without platformio.ini raises MaterialiseError post-extract."""
    tarball = _synthetic_tarball(platformio_ini=None)
    with pytest.raises(MaterialiseError, match=r"missing required 'platformio\.ini'"):
        _materialise_in_tmp(tarball, tmp_path)


def test_materialise_rejects_oversized_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A member declaring more bytes than the cap is rejected as a decompression-bomb defence."""
    monkeypatch.setattr(
        "esphome_device_builder.helpers.remote_artifacts_materialise.FIRMWARE_MAX_TOTAL_BYTES",
        16,
    )
    tarball = _synthetic_tarball()
    with pytest.raises(MaterialiseError, match=r"FIRMWARE_MAX_TOTAL_BYTES"):
        _materialise_in_tmp(tarball, tmp_path)


def test_materialise_rejects_non_dict_idedata(tmp_path: Path) -> None:
    """idedata.json that parses to a non-dict raises MaterialiseError."""
    tarball = _synthetic_tarball(idedata=b"null")
    with pytest.raises(MaterialiseError, match=r"is not a JSON object"):
        _materialise_in_tmp(tarball, tmp_path)
