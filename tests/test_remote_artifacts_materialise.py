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
import os
import sys
import tarfile
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from unittest.mock import patch

import pytest
from esphome.core import CORE

from esphome_device_builder.controllers.remote_build.artifacts_tarball import (
    BUILD_INFO_MEMBER_NAME,
    IDEDATA_MEMBER_NAME,
    PLATFORMIO_INI_MEMBER_NAME,
    STORAGE_MEMBER_NAME,
    VALIDATED_YAML_MEMBER_NAME,
    pack_build_artifacts,
)
from esphome_device_builder.helpers.config_hash import read_build_info_hash
from esphome_device_builder.helpers.remote_artifacts_materialise import (
    MaterialiseError,
    _force_idedata_cache_hit,
    _remap_to_offloader,
    materialise_remote_artifacts,
)
from esphome_device_builder.helpers.storage_path import (
    resolve_compiled_config_path,
    resolve_idedata_path,
    resolve_storage_path,
)
from tests.conftest import HAS_NATIVE_IDF_TOOLCHAIN
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


def test_materialise_lands_build_info_json_for_hash_lookup(
    paired_roots: tuple[Path, Path],
) -> None:
    """#654: build_info.json round-trips so ``read_build_info_hash`` resolves."""
    receiver_root, offloader_root = paired_roots
    config_hash_int = 0x5A94A12D
    tarball = _pack_in_tmp(
        receiver_root,
        extra_build_files={
            BUILD_INFO_MEMBER_NAME: f'{{"config_hash": {config_hash_int}}}\n'.encode(),
        },
    )
    build_path = _materialise_in_tmp(tarball, offloader_root)

    staged = build_path / BUILD_INFO_MEMBER_NAME
    assert staged.is_file()
    assert json.loads(staged.read_text())["config_hash"] == config_hash_int

    sentinel = offloader_root / "___DASHBOARD_SENTINEL___.yaml"
    with patch.object(CORE, "config_path", sentinel):
        yaml_path = offloader_root / "kitchen.yaml"
        assert read_build_info_hash(yaml_path) == "5a94a12d"


def test_materialise_stages_validated_yaml_for_esphome_fast_path(
    paired_roots: tuple[Path, Path],
) -> None:
    """Stage the receiver-side validated-config cache at the offloader's path.

    The next ``esphome upload`` / ``logs`` then skips ``read_config``.
    """
    receiver_root, offloader_root = paired_roots
    cache_body = b"esphome:\n  name: kitchen\n"
    tarball = _pack_in_tmp(receiver_root, validated_yaml=cache_body)
    _materialise_in_tmp(tarball, offloader_root)

    sentinel = offloader_root / "___DASHBOARD_SENTINEL___.yaml"
    with patch.object(CORE, "config_path", sentinel):
        staged = resolve_compiled_config_path("kitchen.yaml")
    assert staged.read_bytes() == cache_body
    # 0600 because the cache resolves !secret inline. POSIX mode bits
    # are inapplicable on Windows -- the offloader's chmod call is a
    # no-op there, so skip the assertion rather than fight ACL shape.
    if sys.platform != "win32":
        assert (staged.stat().st_mode & 0o777) == 0o600


def test_materialise_handles_missing_validated_yaml(
    paired_roots: tuple[Path, Path],
) -> None:
    """Receiver without the cache (old esphome): materialise completes, no cache staged."""
    receiver_root, offloader_root = paired_roots
    tarball = _pack_in_tmp(receiver_root)
    _materialise_in_tmp(tarball, offloader_root)

    sentinel = offloader_root / "___DASHBOARD_SENTINEL___.yaml"
    with patch.object(CORE, "config_path", sentinel):
        staged = resolve_compiled_config_path("kitchen.yaml")
    assert not staged.exists()


def test_pack_skips_stale_validated_yaml_after_esphome_downgrade(
    paired_roots: tuple[Path, Path],
) -> None:
    """Drop a stale validated.yaml at pack time after an esphome downgrade.

    The packer rejects any cache whose mtime predates storage.json by
    more than the threshold, so the offloader doesn't land with a
    cache that no longer matches the binary on disk.
    """
    receiver_root, offloader_root = paired_roots
    cache_body = b"esphome:\n  name: kitchen\n"
    sentinel = receiver_root / "___DASHBOARD_SENTINEL___.yaml"
    with patch.object(CORE, "config_path", sentinel):
        paths = _write_receiver_state(receiver_root, validated_yaml=cache_body)
        # Back-date the cache an hour before the sidecar -- the gap a
        # cross-version downgrade produces.
        now = time.time()
        os.utime(paths["storage_path"], (now, now))
        os.utime(paths["validated_yaml_path"], (now - 3600, now - 3600))
        tarball = pack_build_artifacts("kitchen.yaml").tarball

    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
        assert VALIDATED_YAML_MEMBER_NAME not in tar.getnames()

    _materialise_in_tmp(tarball, offloader_root)
    with patch.object(CORE, "config_path", offloader_root / "___DASHBOARD_SENTINEL___.yaml"):
        assert not resolve_compiled_config_path("kitchen.yaml").exists()


def test_materialise_handles_missing_build_info_json(
    paired_roots: tuple[Path, Path],
) -> None:
    """Receiver without build_info.json: materialise completes, no file staged."""
    receiver_root, offloader_root = paired_roots
    tarball = _pack_in_tmp(receiver_root)
    build_path = _materialise_in_tmp(tarball, offloader_root)
    assert not (build_path / BUILD_INFO_MEMBER_NAME).exists()


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


def test_materialise_preserves_pioenvs_on_same_platform_rerun(
    paired_roots: tuple[Path, Path],
) -> None:
    """Same-platform reruns keep the local build cache for PIO incremental compile."""
    receiver_root, offloader_root = paired_roots
    tarball = _pack_in_tmp(receiver_root)
    first = _materialise_in_tmp(tarball, offloader_root)
    # Plant a file the second materialise must preserve so a
    # local → remote → local cycle keeps PlatformIO's object
    # cache from the prior local build.
    cached = first / ".pioenvs" / "kitchen" / "src.cpp.o"
    cached.write_bytes(b"CACHED-OBJ")

    second = _materialise_in_tmp(tarball, offloader_root)

    assert first == second
    assert (second / ".pioenvs" / "kitchen" / "firmware.bin").is_file()
    assert cached.read_bytes() == b"CACHED-OBJ"


def test_materialise_preserves_platformio_ini_mtime_when_unchanged(
    paired_roots: tuple[Path, Path],
) -> None:
    """Same-content extract restores platformio.ini's prior mtime exactly."""
    receiver_root, offloader_root = paired_roots
    tarball = _pack_in_tmp(receiver_root)
    first = _materialise_in_tmp(tarball, offloader_root)
    pio_ini = first / "platformio.ini"
    fixed_mtime_ns = int((time.time() - 3600) * 1_000_000_000)
    os.utime(pio_ini, ns=(fixed_mtime_ns, fixed_mtime_ns))
    # Re-read so the equality assertion compares against what the FS
    # actually stored after rounding to its native resolution.
    pinned_pio_ns = pio_ini.stat().st_mtime_ns

    obj = first / ".pioenvs" / "kitchen" / "src" / "main.o"
    obj.parent.mkdir(parents=True, exist_ok=True)
    obj.write_bytes(b"OBJ")
    obj_mtime_ns = pinned_pio_ns + 60 * 1_000_000_000
    os.utime(obj, ns=(obj_mtime_ns, obj_mtime_ns))

    _materialise_in_tmp(tarball, offloader_root)

    assert pio_ini.stat().st_mtime_ns == pinned_pio_ns
    assert pio_ini.stat().st_mtime_ns < obj.stat().st_mtime_ns


def test_materialise_bumps_platformio_ini_mtime_when_content_changed(
    paired_roots: tuple[Path, Path], tmp_path: Path
) -> None:
    """Different-content extract bumps platformio.ini's mtime to ~now."""
    receiver_root, offloader_root = paired_roots
    first_tarball = _pack_in_tmp(receiver_root)
    first = _materialise_in_tmp(first_tarball, offloader_root)
    pio_ini = first / "platformio.ini"
    fixed_mtime_ns = int((time.time() - 3600) * 1_000_000_000)
    os.utime(pio_ini, ns=(fixed_mtime_ns, fixed_mtime_ns))
    pinned_pio_ns = pio_ini.stat().st_mtime_ns

    receiver_root_2 = tmp_path / "receiver2"
    receiver_root_2.mkdir()
    sentinel_2 = receiver_root_2 / "___DASHBOARD_SENTINEL___.yaml"
    with patch.object(CORE, "config_path", sentinel_2):
        _write_receiver_state(receiver_root_2)
        receiver_pio = receiver_root_2 / ".esphome" / "build" / "kitchen" / "platformio.ini"
        receiver_pio.write_bytes(
            b"[env:kitchen]\nplatform = espressif32\nbuild_flags = -DCHANGED\n"
        )
        second_tarball = pack_build_artifacts("kitchen.yaml").tarball

    before_ns = time.time_ns()
    _materialise_in_tmp(second_tarball, offloader_root)
    after_ns = time.time_ns()

    assert pio_ini.read_bytes() == receiver_pio.read_bytes()
    post_ns = pio_ini.stat().st_mtime_ns
    assert post_ns != pinned_pio_ns
    assert before_ns <= post_ns <= after_ns


def test_force_idedata_cache_hit_does_not_touch_platformio_ini_mtime(
    paired_roots: tuple[Path, Path],
) -> None:
    """``_force_idedata_cache_hit`` only pushes the idedata mtime forward."""
    receiver_root, offloader_root = paired_roots
    tarball = _pack_in_tmp(receiver_root)
    build_path = _materialise_in_tmp(tarball, offloader_root)
    pio_ini = build_path / "platformio.ini"
    fixed_mtime_ns = int((time.time() - 7200) * 1_000_000_000)
    os.utime(pio_ini, ns=(fixed_mtime_ns, fixed_mtime_ns))
    pinned_pio_ns = pio_ini.stat().st_mtime_ns

    sentinel = offloader_root / "___DASHBOARD_SENTINEL___.yaml"
    with patch.object(CORE, "config_path", sentinel):
        cached = resolve_idedata_path("kitchen.yaml", name="kitchen")
    # Pin idedata to before platformio.ini so the helper has work to do.
    older_ns = pinned_pio_ns - 60 * 1_000_000_000
    os.utime(cached, ns=(older_ns, older_ns))

    _force_idedata_cache_hit(platformio_ini=pio_ini, cached_idedata=cached)

    assert pio_ini.stat().st_mtime_ns == pinned_pio_ns
    assert cached.stat().st_mtime_ns > pio_ini.stat().st_mtime_ns


def test_materialise_wipes_build_tree_on_platform_swap(
    paired_roots: tuple[Path, Path], tmp_path: Path
) -> None:
    """A platform swap that drops the prior platform's component wipes stale artefacts."""
    receiver_root, offloader_root = paired_roots
    first_tarball = _pack_in_tmp(receiver_root, loaded_integrations=["esp32"])
    first = _materialise_in_tmp(first_tarball, offloader_root)
    stale = first / ".pioenvs" / "kitchen" / "stale.bin"
    stale.write_bytes(b"STALE")

    # Re-pack from a fresh receiver root with a different platform; the
    # esp32 → esp8266 swap drops esp32 from loaded_integrations which is
    # the set diff esphome's storage_should_clean keys the wipe on.
    receiver_root_2 = tmp_path / "receiver2"
    receiver_root_2.mkdir()
    swap_tarball = _pack_in_tmp(
        receiver_root_2,
        target_platform="ESP8266",
        loaded_integrations=["esp8266"],
        extra_build_files={".pioenvs/kitchen/firmware.elf": b"ELF"},
    )

    second = _materialise_in_tmp(swap_tarball, offloader_root)

    assert first == second
    assert not stale.exists()
    assert (second / ".pioenvs" / "kitchen" / "firmware.elf").read_bytes() == b"ELF"


def test_materialise_wipes_on_loaded_integrations_removal(
    paired_roots: tuple[Path, Path], tmp_path: Path
) -> None:
    """Dropping a component (sensor → no sensor) on the same platform still wipes."""
    receiver_root, offloader_root = paired_roots
    first_tarball = _pack_in_tmp(receiver_root, loaded_integrations=["esp32", "dht"])
    first = _materialise_in_tmp(first_tarball, offloader_root)
    stale = first / ".pioenvs" / "kitchen" / "stale.bin"
    stale.write_bytes(b"STALE")

    receiver_root_2 = tmp_path / "receiver2"
    receiver_root_2.mkdir()
    shrunk_tarball = _pack_in_tmp(receiver_root_2, loaded_integrations=["esp32"])

    _materialise_in_tmp(shrunk_tarball, offloader_root)

    assert not stale.exists()


def test_materialise_wipes_when_prior_storage_is_corrupt(
    paired_roots: tuple[Path, Path],
) -> None:
    """A corrupt prior sidecar reads back as None and falls through to wipe."""
    receiver_root, offloader_root = paired_roots
    tarball = _pack_in_tmp(receiver_root)
    first = _materialise_in_tmp(tarball, offloader_root)
    stale = first / ".pioenvs" / "kitchen" / "stale.bin"
    stale.write_bytes(b"STALE")

    # Corrupt the staged prior sidecar so StorageJSON.load returns None.
    sentinel = offloader_root / "___DASHBOARD_SENTINEL___.yaml"
    with patch.object(CORE, "config_path", sentinel):
        resolve_storage_path("kitchen.yaml").write_text("{not-json")

    _materialise_in_tmp(tarball, offloader_root)

    assert not stale.exists()


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_materialise_rejects_missing_storage_member(tmp_path: Path) -> None:
    """A tarball without storage.json raises MaterialiseError with a clear message."""
    tarball = _synthetic_tarball(storage=None)
    with pytest.raises(MaterialiseError, match=r"missing required member: 'storage\.json'"):
        _materialise_in_tmp(tarball, tmp_path)


def test_materialise_rejects_pio_tarball_missing_idedata(tmp_path: Path) -> None:
    """A PlatformIO tarball (platformio.ini present) without idedata.json is wire drift."""
    tarball = _synthetic_tarball(idedata=None)
    with pytest.raises(MaterialiseError, match=r"missing required 'idedata\.json' member"):
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


def test_materialise_rejects_pio_tarball_missing_platformio_ini(tmp_path: Path) -> None:
    """A PlatformIO tarball without platformio.ini raises, not silently passed as native-IDF."""
    tarball = _synthetic_tarball(platformio_ini=None)
    with pytest.raises(MaterialiseError, match=r"missing required 'platformio\.ini'"):
        _materialise_in_tmp(tarball, tmp_path)


@pytest.mark.skipif(
    not HAS_NATIVE_IDF_TOOLCHAIN, reason="esphome lacks the native ESP-IDF toolchain (< 2026.5.0)"
)
def test_materialise_native_idf_round_trip_without_pio_metadata(
    paired_roots: tuple[Path, Path],
) -> None:
    """Native ESP-IDF (no platformio.ini / idedata) round-trips; no idedata cache staged."""
    receiver_root, offloader_root = paired_roots
    tarball = _pack_in_tmp(
        receiver_root,
        native_idf=True,
        extra_build_files={
            "build/firmware.factory.bin": b"FACTORY",
            "build/firmware.ota.bin": b"OTA",
            "build/firmware.elf": b"ELF",
        },
    )
    build_path = _materialise_in_tmp(tarball, offloader_root)

    assert build_path == offloader_root / ".esphome" / "build" / "kitchen"
    assert (build_path / "build" / "kitchen.bin").is_file()
    assert (build_path / "build" / "firmware.factory.bin").is_file()
    assert not (build_path / "platformio.ini").exists()
    # Native IDF ships no idedata cache, so the offloader stages none.
    sentinel = offloader_root / "___DASHBOARD_SENTINEL___.yaml"
    with patch.object(CORE, "config_path", sentinel):
        assert not resolve_idedata_path("kitchen.yaml", name="kitchen").exists()


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


def test_materialise_rejects_non_regular_storage_member(tmp_path: Path) -> None:
    """A storage.json entry that's a symlink (not a regular file) is rejected."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name=STORAGE_MEMBER_NAME)
        info.type = tarfile.SYMTYPE
        info.linkname = "../../../etc/passwd"
        tar.addfile(info)
    with pytest.raises(MaterialiseError, match=r"not a regular file"):
        _materialise_in_tmp(buf.getvalue(), tmp_path)


def test_materialise_rejects_non_dict_storage(tmp_path: Path) -> None:
    """storage.json that parses to a non-dict (e.g. ``null``) raises."""
    tarball = _synthetic_tarball(storage=b"null")
    with pytest.raises(MaterialiseError, match=r"is not a JSON object"):
        _materialise_in_tmp(tarball, tmp_path)


def test_materialise_rejects_non_json_idedata(tmp_path: Path) -> None:
    """idedata.json that isn't parseable JSON raises."""
    tarball = _synthetic_tarball(idedata=b"{not-json")
    with pytest.raises(MaterialiseError, match=r"idedata.*not valid JSON"):
        _materialise_in_tmp(tarball, tmp_path)


def test_materialise_rejects_non_dict_idedata(tmp_path: Path) -> None:
    """idedata.json that parses to a non-dict raises MaterialiseError."""
    tarball = _synthetic_tarball(idedata=b"null")
    with pytest.raises(MaterialiseError, match=r"is not a JSON object"):
        _materialise_in_tmp(tarball, tmp_path)


def test_remap_to_offloader_returns_input_when_not_under_build_path(tmp_path: Path) -> None:
    """An absolute path that isn't under receiver_build_path passes through unchanged."""
    receiver_build = tmp_path / "receiver_build"
    offloader_build = tmp_path / "offloader_build"
    outside = "/totally/unrelated/path.bin"
    result = _remap_to_offloader(outside, receiver_build, offloader_build)
    assert result == Path(outside)


_WIN_BUILD_PATH = r"C:\Users\receiver\esphome\.esphome\.remote_builds\_pin\.esphome\build\dev"


def _windows_storage_dict(*, build_path: str, firmware_bin_path: str) -> dict[str, Any]:
    """Receiver-side ``storage.json`` shape carrying Windows-absolute paths."""
    return {
        "storage_version": 1,
        "name": "dev",
        "esp_platform": "ESP32",
        "build_path": build_path,
        "firmware_bin_path": firmware_bin_path,
        "loaded_integrations": [],
        "loaded_platforms": [],
        "no_mdns": False,
        "framework": "arduino",
        "core_platform": "esp32",
    }


@pytest.mark.parametrize(
    ("receiver_build", "receiver_firmware"),
    [
        pytest.param(
            PureWindowsPath(_WIN_BUILD_PATH),
            _WIN_BUILD_PATH + r"\.pioenvs\dev\firmware.bin",
            id="windows_receiver",
        ),
        pytest.param(
            PurePosixPath("/home/builder/.esphome/.remote_builds/pin/build/dev"),
            "/home/builder/.esphome/.remote_builds/pin/build/dev/.pioenvs/dev/firmware.bin",
            id="posix_receiver",
        ),
    ],
)
def test_remap_to_offloader_translates_receiver_path_across_os(
    tmp_path: Path, receiver_build: Any, receiver_firmware: str
) -> None:
    """Receiver path under receiver build remaps under offloader build for either flavour."""
    offloader_build = tmp_path / "build" / "dev"
    result = _remap_to_offloader(receiver_firmware, receiver_build, offloader_build)
    assert result == offloader_build / ".pioenvs" / "dev" / "firmware.bin"


def test_remap_to_offloader_rejects_dotdot_traversal_in_receiver_path(tmp_path: Path) -> None:
    """A ``..`` segment in the computed relative raises MaterialiseError."""
    receiver_build = PureWindowsPath(_WIN_BUILD_PATH)
    offloader_build = tmp_path / "build" / "dev"
    malicious = _WIN_BUILD_PATH + r"\..\..\..\Windows\System32\evil.bin"
    with pytest.raises(MaterialiseError, match=r"contains '\.\.'"):
        _remap_to_offloader(malicious, receiver_build, offloader_build)


def test_materialise_windows_receiver_storage_remaps_firmware_bin_path(tmp_path: Path) -> None:
    """Windows-flavoured tarball metadata materialises to offloader-local paths on disk."""
    win_firmware_bin = _WIN_BUILD_PATH + r"\.pioenvs\dev\firmware.bin"
    storage = _windows_storage_dict(build_path=_WIN_BUILD_PATH, firmware_bin_path=win_firmware_bin)
    idedata = {
        "prog_path": _WIN_BUILD_PATH + r"\.pioenvs\dev\firmware.elf",
        "extra": {
            "flash_images": [
                {
                    "path": _WIN_BUILD_PATH + r"\.pioenvs\dev\bootloader.bin",
                    "offset": "0x1000",
                }
            ]
        },
    }
    tarball = _synthetic_tarball(
        storage=storage,
        idedata=idedata,
        extra_members=[
            (".pioenvs/dev/firmware.bin", b"FIRMWARE"),
            (".pioenvs/dev/firmware.factory.bin", b"FACTORY"),
            (".pioenvs/dev/bootloader.bin", b"BOOT"),
        ],
    )
    build_path = _materialise_in_tmp(tarball, tmp_path, configuration="dev.yaml")

    staged_storage = json.loads(resolve_storage_path("dev.yaml").read_bytes())
    assert staged_storage["build_path"] == str(build_path)
    expected_firmware_bin = build_path / ".pioenvs" / "dev" / "firmware.bin"
    assert staged_storage["firmware_bin_path"] == str(expected_firmware_bin)

    # firmware/download path: base_dir = firmware_bin_path.parent.
    factory_bin = Path(staged_storage["firmware_bin_path"]).parent / "firmware.factory.bin"
    assert factory_bin.is_file()
    assert factory_bin.read_bytes() == b"FACTORY"

    staged_idedata = json.loads(resolve_idedata_path("dev.yaml", name="dev").read_bytes())
    assert staged_idedata["prog_path"] == str(build_path / ".pioenvs" / "dev" / "firmware.elf")
    assert staged_idedata["extra"]["flash_images"][0]["path"] == str(
        build_path / ".pioenvs" / "dev" / "bootloader.bin"
    )


def test_materialise_rejects_dotdot_in_receiver_storage(tmp_path: Path) -> None:
    """A malicious receiver shipping ``..`` in ``firmware_bin_path`` is rejected."""
    storage = _windows_storage_dict(
        build_path=_WIN_BUILD_PATH,
        firmware_bin_path=_WIN_BUILD_PATH + r"\..\..\..\Windows\System32\evil.bin",
    )
    tarball = _synthetic_tarball(storage=storage)
    with pytest.raises(MaterialiseError, match=r"contains '\.\.'"):
        _materialise_in_tmp(tarball, tmp_path, configuration="dev.yaml")


def test_materialise_rejects_cumulative_member_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two extract-side members whose sum breaches the cap raise on the second."""
    monkeypatch.setattr(
        "esphome_device_builder.helpers.remote_artifacts_materialise.FIRMWARE_MAX_TOTAL_BYTES",
        500,
    )
    # storage / idedata read via _read_member_required (single-member
    # cap only). The cumulative gate fires in _safe_extract_excluding
    # across build-tree members; ship two that exceed the cap together.
    tarball = _synthetic_tarball(
        extra_members=[
            (".pioenvs/kitchen/firmware.bin", b"x" * 300),
            (".pioenvs/kitchen/firmware.elf", b"y" * 300),
        ]
    )
    with pytest.raises(MaterialiseError, match=r"cumulative size"):
        _materialise_in_tmp(tarball, tmp_path)


def test_materialise_rejects_cumulative_metadata_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Metadata reads share one running cap so a hostile peer can't stuff each member.

    storage.json + idedata.json each fit under the cap individually
    but together breach it; the threaded running total catches the
    breach on the second read.
    """
    monkeypatch.setattr(
        "esphome_device_builder.helpers.remote_artifacts_materialise.FIRMWARE_MAX_TOTAL_BYTES",
        500,
    )
    bloated = b"x" * 300
    tarball = _synthetic_tarball(
        storage=bloated,
        idedata=bloated,
    )
    with pytest.raises(MaterialiseError, match=r"cumulative size"):
        _materialise_in_tmp(tarball, tmp_path)


def test_materialise_rejects_unreadable_storage_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defensive guard: a regular-file member whose ``extractfile`` returns None raises."""
    tarball = _synthetic_tarball()
    real_extractfile = tarfile.TarFile.extractfile

    def _stub_extractfile(self: tarfile.TarFile, member: object) -> object:
        # Force None for the storage.json read; let other reads pass.
        info = member if isinstance(member, tarfile.TarInfo) else self.getmember(str(member))
        if info.name == STORAGE_MEMBER_NAME:
            return None
        return real_extractfile(self, member)

    monkeypatch.setattr(tarfile.TarFile, "extractfile", _stub_extractfile)
    with pytest.raises(MaterialiseError, match=r"unreadable"):
        _materialise_in_tmp(tarball, tmp_path)


def test_materialise_raises_when_storage_load_returns_none(
    paired_roots: tuple[Path, Path],
) -> None:
    """A storage payload missing ``storage_version`` makes ``StorageJSON.load`` return None."""
    _, offloader_root = paired_roots
    # Drop storage_version so the early _parse_storage_json path still
    # accepts name + build_path, but esphome.storage_json._load_impl
    # raises KeyError and StorageJSON.load returns None.
    tarball = _synthetic_tarball(storage={"name": "kitchen", "build_path": _FAKE_BUILD_PATH})
    with pytest.raises(MaterialiseError, match=r"StorageJSON\.load returned None"):
        _materialise_in_tmp(tarball, offloader_root)


def test_materialise_idedata_skips_non_dict_flash_image_entry(
    paired_roots: tuple[Path, Path],
) -> None:
    """A non-dict entry in idedata.extra.flash_images is silently skipped."""
    receiver_root, offloader_root = paired_roots
    sentinel = receiver_root / "___DASHBOARD_SENTINEL___.yaml"
    with patch.object(CORE, "config_path", sentinel):
        _write_receiver_state(receiver_root)
        # Inject a malformed flash_images entry alongside a valid one.
        idedata_path = resolve_idedata_path("kitchen.yaml", name="kitchen")
        data = json.loads(idedata_path.read_text())
        data.setdefault("extra", {})["flash_images"] = [
            "not-a-dict",
            {"path": "/fake/receiver/build/firmware.bin"},
        ]
        idedata_path.write_text(json.dumps(data) + "\n")
        packed = pack_build_artifacts("kitchen.yaml")

    _materialise_in_tmp(packed.tarball, offloader_root)

    sentinel = offloader_root / "___DASHBOARD_SENTINEL___.yaml"
    with patch.object(CORE, "config_path", sentinel):
        cached = resolve_idedata_path("kitchen.yaml", name="kitchen")
    data = json.loads(cached.read_text())
    # The non-dict survives untouched; the dict entry got remapped.
    flash_images = data["extra"]["flash_images"]
    assert flash_images[0] == "not-a-dict"
    assert isinstance(flash_images[1], dict)


def test_materialise_tolerates_pre_extract_rmtree_failure(
    paired_roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Windows-style rmtree failure on the build dir doesn't unwind the materialise."""
    receiver_root, offloader_root = paired_roots
    tarball = _pack_in_tmp(receiver_root)

    # First seed an existing build dir + stale file so the wipe
    # path actually runs (mkdir + extract would otherwise create
    # everything fresh on first call).
    stale_build_path = offloader_root / ".esphome" / "build" / "kitchen"
    stale_build_path.mkdir(parents=True)
    (stale_build_path / "stale.bin").write_bytes(b"stale")

    def _flaky(path: object, *args: object, **kwargs: object) -> None:
        raise PermissionError("simulated read-only file on windows")

    monkeypatch.setattr(
        "esphome_device_builder.helpers.remote_artifacts_materialise.rmtree", _flaky
    )

    # Materialise must not raise; extract proceeds on top of the
    # un-wiped dir (the mkdir is idempotent, the tar extract
    # overwrites file-by-file).
    build_path = _materialise_in_tmp(tarball, offloader_root)
    assert (build_path / "platformio.ini").is_file()


def test_force_idedata_cache_hit_noop_when_files_missing(tmp_path: Path) -> None:
    """``_force_idedata_cache_hit`` returns early when either side doesn't exist."""
    # Neither file exists; helper must not raise.
    _force_idedata_cache_hit(
        platformio_ini=tmp_path / "missing.ini",
        cached_idedata=tmp_path / "missing.json",
    )
