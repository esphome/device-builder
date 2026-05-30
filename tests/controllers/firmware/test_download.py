"""Tests for ``FirmwareController.download``.

The handler reads a compiled firmware binary off disk and returns
it base64-encoded for Web Serial flashing. Coverage targets the
five branches that decide what comes back:

- Storage sidecar missing → ``FileNotFoundError("No firmware binary…")``.
- Sidecar present but ``firmware_bin_path`` unset (compile never
  reached the link stage) → same ``FileNotFoundError``.
- ``file`` argument resolving outside the build dir → traversal
  guard raises ``ValueError`` (``Path.relative_to`` semantics).
- Requested binary not present on disk → ``FileNotFoundError("Binary
  not found: …")``.
- Happy path uncompressed and compressed — returns the right
  filename / base64 data / size / compressed flag.

Configuration-level traversal (the ``await
_validate_configuration_boundary`` line at the top of the handler)
is already covered in ``test_traversal_validation.py``; this file
is only about what happens once the configuration argument
validates.
"""

from __future__ import annotations

import base64
import gzip
from pathlib import Path
from typing import Any

import pytest

import esphome_device_builder.controllers.firmware.download as download_module
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode
from tests._storage_fixtures import write_storage_json
from tests.controllers.firmware.conftest import FirmwareControllerFactory


@pytest.fixture(autouse=True)
def _redirect_ext_storage_path(monkeypatch: Any, tmp_path: Path) -> None:
    """Pin ``ext_storage_path`` at ``<tmp>/.esphome/storage/<config>.json``.

    The real ``ext_storage_path`` derives from ``CORE.config_path``,
    which isn't initialised in the test process. Both the firmware
    controller and its helpers import it independently, so patch the
    binding in the controller submodule (the only one ``download``
    reads through).
    """
    monkeypatch.setattr(
        "esphome_device_builder.controllers.firmware.download.resolve_storage_path",
        lambda configuration: tmp_path / ".esphome" / "storage" / f"{configuration}.json",
    )


def _make_firmware(build_dir: Path, name: str, payload: bytes) -> Path:
    """Lay out a ``firmware.bin`` inside *build_dir* and return its path."""
    build_dir.mkdir(parents=True, exist_ok=True)
    fw = build_dir / name
    fw.write_bytes(payload)
    return fw


# ---------------------------------------------------------------------------
# Failure branches
# ---------------------------------------------------------------------------


async def test_download_raises_when_storage_missing(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """No StorageJSON sidecar at all → user hasn't compiled this device yet.

    The storage sidecar is the only place the dashboard knows where
    the firmware bin lives; without it there's nothing to serve.
    Surface the actionable error rather than letting an opaque ``None``
    crash deeper in the handler.
    """
    controller = firmware_controller_factory()

    with pytest.raises(FileNotFoundError, match="No firmware binary"):
        await controller.download(configuration="kitchen.yaml", file="firmware.bin")


async def test_download_raises_when_firmware_bin_path_unset(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """Sidecar exists but ``firmware_bin_path`` is None → same actionable error.

    Happens when the compile aborted before the link stage (e.g.
    syntax error caught by validate, missing toolchain) — the
    sidecar got written by ``--only-generate`` but the bin never
    landed. Same error message as the missing-sidecar case so the
    frontend handles both identically.
    """
    write_storage_json(tmp_path, "kitchen.yaml", firmware_bin_path=None)
    controller = firmware_controller_factory()

    with pytest.raises(FileNotFoundError, match="No firmware binary"):
        await controller.download(configuration="kitchen.yaml", file="firmware.bin")


async def test_download_raises_on_traversal_in_file(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """A ``file`` value escaping the build dir trips ``Path.relative_to``.

    The traversal guard exists because ``file`` reaches the handler
    from a WS request — a malicious client could pass
    ``"../../../etc/passwd"`` and read host files. ``relative_to``
    raises ``ValueError`` when the resolved path falls outside the
    build dir; the handler doesn't translate it because the guard
    is defense-in-depth — the legitimate frontend never sends a
    traversal-shaped ``file``.
    """
    build_dir = tmp_path / ".esphome" / "build" / "kitchen"
    fw = _make_firmware(build_dir, "firmware.bin", b"\x00" * 16)
    write_storage_json(tmp_path, "kitchen.yaml", firmware_bin_path=fw)
    # Drop a file outside build_dir that the traversal would reach.
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    controller = firmware_controller_factory()

    with pytest.raises(ValueError):
        await controller.download(configuration="kitchen.yaml", file="../../../secret.txt")


async def test_download_raises_when_binary_missing(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """Sidecar + build dir present but the requested file isn't there.

    Distinct from ``firmware_bin_path`` being unset: the compile
    succeeded but the user asked for a binary the build didn't emit
    (e.g. ``firmware-factory.bin`` on a board that only emits the
    OTA bin). Returns the actionable per-file error.
    """
    build_dir = tmp_path / ".esphome" / "build" / "kitchen"
    fw = _make_firmware(build_dir, "firmware.bin", b"\x00" * 16)
    write_storage_json(tmp_path, "kitchen.yaml", firmware_bin_path=fw)
    controller = firmware_controller_factory()

    with pytest.raises(FileNotFoundError, match=r"Binary not found: missing\.bin"):
        await controller.download(configuration="kitchen.yaml", file="missing.bin")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_download_returns_base64_payload_uncompressed(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """Default path returns ``{filename, data, size, compressed=False}``.

    ``filename`` is ``<storage.name>-<file>``; ``data`` is the
    base64-encoded raw bytes; ``size`` is the *encoded* payload's
    pre-base64 length; ``compressed`` is False.
    """
    payload = b"firmware bytes \x01\x02\x03\x04"
    build_dir = tmp_path / ".esphome" / "build" / "kitchen"
    fw = _make_firmware(build_dir, "firmware.bin", payload)
    write_storage_json(tmp_path, "kitchen.yaml", firmware_bin_path=fw)
    controller = firmware_controller_factory()

    result = await controller.download(configuration="kitchen.yaml", file="firmware.bin")

    assert result["filename"] == "kitchen-firmware.bin"
    assert result["compressed"] is False
    assert result["size"] == len(payload)
    assert base64.b64decode(result["data"]) == payload


async def test_download_validator_runs_before_ext_storage_path(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """``ext_storage_path`` is unsafe in isolation; the validator gate matters.

    Pinning this so a future refactor that drops or re-orders
    ``_validate_configuration_boundary`` in ``download`` immediately
    surfaces the regression.

    The function in upstream esphome is literally
    ``CORE.data_dir / "storage" / f"{config_filename}.json"`` — no
    sanitisation, no ``relative_to`` check. A traversal-shaped
    configuration (``"../../../etc/passwd"``) feeds straight through
    and would resolve outside the storage tree if the call ever ran.

    The handler avoids that by validating ``configuration`` first
    via ``rel_path`` (raises ``CommandError(INVALID_ARGS)``), so a
    traversal payload trips the gate and never reaches the
    ``ext_storage_path`` call inside the executor closure. We assert
    both halves: the handler raises a ``CommandError``, and the
    autouse ``ext_storage_path`` redirect is never invoked.
    """
    invocations: list[str] = []

    def _spy(configuration: str) -> Path:
        invocations.append(configuration)
        return tmp_path / ".esphome" / "storage" / f"{configuration}.json"

    # Replace the autouse redirect with our spy for this test.
    download_module.resolve_storage_path = _spy

    controller = firmware_controller_factory()
    with pytest.raises(CommandError) as excinfo:
        await controller.download(configuration="../../etc/passwd", file="firmware.bin")
    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    # Validator short-circuited *before* the executor closure ran —
    # ``ext_storage_path`` was never invoked with the traversal value.
    assert invocations == []


async def test_download_returns_gzipped_payload_when_compressed(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """``compressed=True`` gzips the bytes and tacks ``.gz`` onto the filename.

    Web Serial flashing skips this path — it asks for raw bytes and
    decodes the base64 itself. The gz path is for the legacy HTTP
    download flow where the browser receives a real ``.bin.gz`` so
    a download manager can stream it to disk under the right name.
    """
    payload = b"larger payload to compress: " + (b"abc" * 200)
    build_dir = tmp_path / ".esphome" / "build" / "kitchen"
    fw = _make_firmware(build_dir, "firmware.bin", payload)
    write_storage_json(tmp_path, "kitchen.yaml", firmware_bin_path=fw)
    controller = firmware_controller_factory()

    result = await controller.download(
        configuration="kitchen.yaml", file="firmware.bin", compressed=True
    )

    assert result["filename"] == "kitchen-firmware.bin.gz"
    assert result["compressed"] is True
    decoded = base64.b64decode(result["data"])
    assert gzip.decompress(decoded) == payload
    assert result["size"] == len(decoded)
