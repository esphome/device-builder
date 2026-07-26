"""Tests for the bundle-import staging path."""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import stat
import sys
import tarfile
from pathlib import Path

import pytest

from esphome_device_builder.controllers.config import (
    get_device_metadata,
    set_device_metadata,
)
from esphome_device_builder.controllers.devices import mutations_import_bundle
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode

from .conftest import MakeControllerFactory

MAIN_YAML = (
    "esphome:\n"
    "  name: kitchen\n"
    "  friendly_name: Kitchen\n"
    "esp32:\n"
    "  variant: esp32\n"
    "  board: nodemcu-32s\n"
)


def _make_bundle(
    files: dict[str, str | bytes],
    *,
    config_filename: str,
    has_secrets: bool = False,
    esphome_version: str = "2026.6.0",
    manifest_version: int = 1,
) -> bytes:
    """Build an ``esphome bundle``-shaped ``.tar.gz`` from *files*."""
    manifest = {
        "manifest_version": manifest_version,
        "esphome_version": esphome_version,
        "config_filename": config_filename,
        "files": list(files),
        "has_secrets": has_secrets,
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:

        def _add(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

        _add("manifest.json", json.dumps(manifest).encode("utf-8"))
        for name, content in files.items():
            _add(name, content if isinstance(content, bytes) else content.encode("utf-8"))
    return buf.getvalue()


@pytest.fixture
def _bundle_storage_under_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the StorageJSON sidecar under ``tmp_path`` instead of CORE.data_dir."""
    storage_dir = tmp_path / ".esphome" / "storage"

    def _resolve(configuration: str) -> Path:
        return storage_dir / f"{configuration}.json"

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.mutations_create.resolve_storage_path",
        _resolve,
    )


async def test_import_bundle_token_mints_a_consumable_token(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """The WS command mints a single-use token the HTTP route can consume."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)

    result = await ctrl.import_bundle_token()

    assert ctrl.import_tokens.consume(result["token"]) is True


@pytest.mark.usefixtures("stub_create_device_metadata_helpers", "_bundle_storage_under_tmp")
async def test_import_bundle_lands_full_tree(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A clean import writes the main YAML, includes, and secrets, then scans."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    bundle = _make_bundle(
        {
            "kitchen.yaml": MAIN_YAML,
            "common/wifi.yaml": "wifi:\n  ssid: home\n",
            "secrets.yaml": "wifi_password: hunter2\n",
        },
        config_filename="kitchen.yaml",
        has_secrets=True,
    )

    result = await ctrl.import_bundle(bundle_bytes=bundle)

    assert result.status == "imported"
    assert result.configuration == "kitchen.yaml"
    assert result.has_secrets is True
    assert result.esphome_version == "2026.6.0"
    assert (tmp_path / "kitchen.yaml").read_text("utf-8") == MAIN_YAML
    assert (tmp_path / "common" / "wifi.yaml").read_text("utf-8") == "wifi:\n  ssid: home\n"
    assert (tmp_path / "secrets.yaml").read_text("utf-8") == "wifi_password: hunter2\n"
    # manifest.json is never written into the config dir.
    assert not (tmp_path / "manifest.json").exists()
    assert ctrl._scanner.calls == [("scan",)]


@pytest.mark.usefixtures("stub_create_device_metadata_helpers", "_bundle_storage_under_tmp")
async def test_import_bundle_reports_conflicts_without_writing(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """An existing target file is reported as a conflict; nothing is touched."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    (tmp_path / "kitchen.yaml").write_text("OLD\n", "utf-8")
    bundle = _make_bundle(
        {"kitchen.yaml": MAIN_YAML, "common/wifi.yaml": "wifi:\n  ssid: home\n"},
        config_filename="kitchen.yaml",
    )

    result = await ctrl.import_bundle(bundle_bytes=bundle)

    assert result.status == "conflicts"
    assert result.conflicts == ["kitchen.yaml"]
    # Nothing written: existing file untouched, no include placed, no scan.
    assert (tmp_path / "kitchen.yaml").read_text("utf-8") == "OLD\n"
    assert not (tmp_path / "common" / "wifi.yaml").exists()
    assert ctrl._scanner.calls == []


@pytest.mark.usefixtures("stub_create_device_metadata_helpers", "_bundle_storage_under_tmp")
async def test_import_bundle_overwrite_replaces_only_chosen_files(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """The second pass overwrites listed files and leaves the rest untouched."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    (tmp_path / "kitchen.yaml").write_text("OLD\n", "utf-8")
    (tmp_path / "common").mkdir()
    (tmp_path / "common" / "wifi.yaml").write_text("KEEP\n", "utf-8")
    bundle = _make_bundle(
        {"kitchen.yaml": MAIN_YAML, "common/wifi.yaml": "wifi:\n  ssid: home\n"},
        config_filename="kitchen.yaml",
    )

    result = await ctrl.import_bundle(bundle_bytes=bundle, overwrite=["kitchen.yaml"])

    assert result.status == "imported"
    # kitchen.yaml was chosen for overwrite; the include was not.
    assert (tmp_path / "kitchen.yaml").read_text("utf-8") == MAIN_YAML
    assert (tmp_path / "common" / "wifi.yaml").read_text("utf-8") == "KEEP\n"
    # The response reports the partial import honestly.
    assert result.written == ["kitchen.yaml"]
    assert result.kept == ["common/wifi.yaml"]
    assert ctrl._scanner.calls == [("scan",)]


@pytest.mark.skipif(sys.platform == "win32", reason="Windows doesn't honor POSIX mode bits")
@pytest.mark.usefixtures("stub_create_device_metadata_helpers", "_bundle_storage_under_tmp")
async def test_import_bundle_overwrite_preserves_operator_mode(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A confirmed bundle overwrite keeps the target's tightened mode."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    target = tmp_path / "kitchen.yaml"
    target.write_text("OLD\n", "utf-8")
    target.chmod(0o600)
    bundle = _make_bundle({"kitchen.yaml": MAIN_YAML}, config_filename="kitchen.yaml")

    result = await ctrl.import_bundle(bundle_bytes=bundle, overwrite=["kitchen.yaml"])

    assert result.status == "imported"
    assert target.read_text("utf-8") == MAIN_YAML
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.usefixtures("stub_create_device_metadata_helpers", "_bundle_storage_under_tmp")
async def test_import_bundle_empty_overwrite_keeps_all_and_reports_it(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """``overwrite=[]`` is a resolved pass that keeps every conflict, reported in ``kept``."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    (tmp_path / "kitchen.yaml").write_text("OLD\n", "utf-8")
    bundle = _make_bundle(
        {"kitchen.yaml": MAIN_YAML, "common/new.yaml": "x\n"},
        config_filename="kitchen.yaml",
    )

    result = await ctrl.import_bundle(bundle_bytes=bundle, overwrite=[])

    assert result.status == "imported"
    # The existing main config was kept (not silently masked as a full import).
    assert (tmp_path / "kitchen.yaml").read_text("utf-8") == "OLD\n"
    assert result.kept == ["kitchen.yaml"]
    # The non-conflicting include still landed.
    assert (tmp_path / "common" / "new.yaml").read_text("utf-8") == "x\n"
    assert result.written == ["common/new.yaml"]


async def test_import_bundle_overwrite_main_preserves_metadata(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Overwriting the main config keeps the device's labels / comment / board_id."""
    config_dir = tmp_path
    (config_dir / "kitchen.yaml").write_text("OLD\n", "utf-8")
    await asyncio.to_thread(
        set_device_metadata,
        config_dir,
        "kitchen.yaml",
        labels=["lab"],
        comment="note",
        board_id="esp32-pick",
        board_id_user_set=True,
    )
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    bundle = _make_bundle({"kitchen.yaml": MAIN_YAML}, config_filename="kitchen.yaml")

    result = await ctrl.import_bundle(bundle_bytes=bundle, overwrite=["kitchen.yaml"])

    assert result.status == "imported"
    assert (config_dir / "kitchen.yaml").read_text("utf-8") == MAIN_YAML
    post = await asyncio.to_thread(get_device_metadata, config_dir, "kitchen.yaml")
    assert post.get("labels") == ["lab"]
    assert post.get("comment") == "note"
    assert post.get("board_id") == "esp32-pick"
    assert ctrl._scanner.calls == [("scan",)]


@pytest.mark.usefixtures("stub_create_device_metadata_helpers", "_bundle_storage_under_tmp")
async def test_import_bundle_merges_secrets_keeping_existing(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Secrets merge in absent keys, keep existing values, and never conflict."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    (tmp_path / "secrets.yaml").write_text("wifi_password: original\n", "utf-8")
    bundle = _make_bundle(
        {
            "kitchen.yaml": MAIN_YAML,
            "secrets.yaml": "wifi_password: from_bundle\napi_key: new_key\n",
        },
        config_filename="kitchen.yaml",
        has_secrets=True,
    )

    result = await ctrl.import_bundle(bundle_bytes=bundle)

    assert result.status == "imported"
    merged = (tmp_path / "secrets.yaml").read_text("utf-8")
    assert "wifi_password: original" in merged  # existing value kept
    assert "api_key: new_key" in merged  # absent key added
    assert "from_bundle" not in merged


@pytest.mark.usefixtures("stub_create_device_metadata_helpers", "_bundle_storage_under_tmp")
async def test_import_bundle_secrets_merge_preserves_comments(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """The merge appends absent keys without reformatting the existing file."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    (tmp_path / "secrets.yaml").write_text(
        "# my secrets\nwifi_password: original  # home net\n", "utf-8"
    )
    bundle = _make_bundle(
        {"kitchen.yaml": MAIN_YAML, "secrets.yaml": "wifi_password: x\napi_key: new_key\n"},
        config_filename="kitchen.yaml",
        has_secrets=True,
    )

    result = await ctrl.import_bundle(bundle_bytes=bundle)

    assert result.status == "imported"
    merged = (tmp_path / "secrets.yaml").read_text("utf-8")
    assert "# my secrets" in merged  # comment preserved
    assert "wifi_password: original  # home net" in merged  # existing line untouched
    assert "api_key: new_key" in merged  # absent key appended


async def test_import_bundle_rejects_non_gzip(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A payload without the gzip header is refused before staging."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)

    with pytest.raises(CommandError) as excinfo:
        await ctrl.import_bundle(bundle_bytes=b"esphome:\n  name: x\n")

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "gzip" in excinfo.value.message


async def test_import_bundle_rejects_oversize(
    tmp_path: Path, make_controller: MakeControllerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundle over the shared byte cap is rejected before staging."""
    monkeypatch.setattr(mutations_import_bundle, "BUNDLE_MAX_TOTAL_BYTES", 4)
    ctrl = make_controller(tmp_path, with_state_monitor=True)

    with pytest.raises(CommandError) as excinfo:
        await ctrl.import_bundle(bundle_bytes=b"\x1f\x8b" + b"x" * 100)

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "exceeds" in excinfo.value.message


async def test_import_bundle_rejects_malformed_tar(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Valid gzip that isn't a tar archive surfaces as INVALID_ARGS."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)

    with pytest.raises(CommandError) as excinfo:
        await ctrl.import_bundle(bundle_bytes=gzip.compress(b"not a tar"))

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert ctrl._scanner.calls == []


async def test_import_bundle_rejects_non_utf8_main_config(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A non-UTF-8 main config is refused before any file is placed."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    bundle = _make_bundle(
        {"kitchen.yaml": b"\xff\xfe\x00not utf8", "common/extra.yaml": "x\n"},
        config_filename="kitchen.yaml",
    )

    with pytest.raises(CommandError) as excinfo:
        await ctrl.import_bundle(bundle_bytes=bundle)

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "UTF-8" in excinfo.value.message
    # Nothing was placed and no device registered.
    assert not (tmp_path / "kitchen.yaml").exists()
    assert not (tmp_path / "common").exists()
    assert ctrl._scanner.calls == []


async def test_import_bundle_rejects_path_traversal(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A traversal member trips upstream extraction, surfaced as INVALID_ARGS."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    bundle = _make_bundle(
        {"kitchen.yaml": MAIN_YAML, "../evil.yaml": "x\n"},
        config_filename="kitchen.yaml",
    )

    with pytest.raises(CommandError) as excinfo:
        await ctrl.import_bundle(bundle_bytes=bundle)

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert ctrl._scanner.calls == []


@pytest.mark.usefixtures("_bundle_storage_under_tmp")
def test_init_bundle_storage_skips_when_main_config_unreadable(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An unreadable main config writes no (degraded) sidecar and logs; no crash."""
    (tmp_path / "binary.yaml").write_bytes(b"\xff\xfe\x00not utf8")

    with caplog.at_level("WARNING"):
        # Missing file and non-UTF-8 file both hit the OSError/decode safety net.
        mutations_import_bundle._init_bundle_storage(tmp_path, "ghost.yaml")
        mutations_import_bundle._init_bundle_storage(tmp_path, "binary.yaml")

    # No degraded sidecar is persisted; the scanner re-derives metadata.
    assert not (tmp_path / ".esphome" / "storage" / "ghost.yaml.json").exists()
    assert not (tmp_path / ".esphome" / "storage" / "binary.yaml.json").exists()
    assert any("ghost.yaml" in r.message for r in caplog.records)
    assert any("binary.yaml" in r.message for r in caplog.records)


@pytest.mark.usefixtures("stub_create_device_metadata_helpers", "_bundle_storage_under_tmp")
async def test_import_bundle_substitution_friendly_name(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A templated ``friendly_name: ${...}`` doesn't leak into the sidecar."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    main = "esphome:\n  name: kitchen\n  friendly_name: ${fn}\nesp32:\n  board: nodemcu-32s\n"
    bundle = _make_bundle({"kitchen.yaml": main}, config_filename="kitchen.yaml")

    result = await ctrl.import_bundle(bundle_bytes=bundle)

    assert result.status == "imported"
    assert (tmp_path / "kitchen.yaml").read_text("utf-8") == main
