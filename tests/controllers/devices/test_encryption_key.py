"""Tests for the HA-provisioned encryption-key handoff (``set_encryption_key``)."""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from esphome_device_builder.controllers.devices._pending_keys_store import PendingKeysStore
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.storage import drain_shutdown_callbacks
from esphome_device_builder.models import ErrorCode
from tests.conftest import make_device

from .conftest import MakeControllerFactory

KEY = base64.b64encode(b"k" * 32).decode()
OTHER_KEY = base64.b64encode(b"j" * 32).decode()

API_KEY_YAML = f"""\
esphome:
  name: kitchen

api:
  encryption:
    key: "{OTHER_KEY}"

wifi:
  ssid: x
"""


def _configure(
    ctrl,
    tmp_path: Path,
    yaml_text: str,
    *,
    name: str = "kitchen",
    **device_overrides,
):
    """Seed one configured device with *yaml_text* on disk."""
    (tmp_path / f"{name}.yaml").write_text(yaml_text, encoding="utf-8")
    device = make_device(name, **device_overrides)
    ctrl._scanner._devices_by_name[name] = [device]
    ctrl._scanner.devices = [device]
    return device


async def test_set_encryption_key_overwrites_existing_literal(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """The pushed key replaces a stale literal (the competing-key dead-end fix)."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _configure(ctrl, tmp_path, API_KEY_YAML)

    result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result == {"result": "updated", "configurations": ["kitchen.yaml"]}
    new_yaml = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert f'key: "{KEY}"' in new_yaml
    assert OTHER_KEY not in new_yaml
    assert ("request", "kitchen.yaml") in ctrl._scanner.calls


async def test_set_encryption_key_same_key_is_unchanged_no_write(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Idempotent re-push reports ``unchanged`` and never touches disk."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    yaml_text = API_KEY_YAML.replace(OTHER_KEY, KEY)
    _configure(ctrl, tmp_path, yaml_text)
    before = (tmp_path / "kitchen.yaml").stat().st_mtime_ns

    result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result == {"result": "unchanged", "configurations": ["kitchen.yaml"]}
    assert (tmp_path / "kitchen.yaml").stat().st_mtime_ns == before


async def test_set_encryption_key_inserts_block_for_package_provided_api(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """No ``api:`` in the YAML but the package loads it → block inserted locally."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    yaml_text = "substitutions:\n  name: kitchen\n\npackages:\n  v: github://x/y.yaml\n"
    _configure(ctrl, tmp_path, yaml_text, api_enabled=True)

    result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result["result"] == "updated"
    new_yaml = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert new_yaml.startswith(f'api:\n  encryption:\n    key: "{KEY}"\n')
    assert yaml_text in new_yaml


async def test_set_encryption_key_refuses_apiless_configuration(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """No ``api:`` block and no loaded ``api`` integration → refuse, don't enable the API."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    yaml_text = "esphome:\n  name: kitchen\n\nmqtt:\n  broker: b\n"
    _configure(ctrl, tmp_path, yaml_text, loaded_integrations=["mqtt", "wifi"], api_enabled=False)

    result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result["result"] == "not_writable"
    assert "native API" in result["reason"]
    assert (tmp_path / "kitchen.yaml").read_text(encoding="utf-8") == yaml_text


async def test_set_encryption_key_refuses_secret_indirection(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """A ``!secret`` key is user-managed material; refuse and report."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    yaml_text = "esphome:\n  name: kitchen\n\napi:\n  encryption:\n    key: !secret api_key\n"
    _configure(ctrl, tmp_path, yaml_text)

    result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result["result"] == "not_writable"
    assert "!secret" in result["reason"]
    assert (tmp_path / "kitchen.yaml").read_text(encoding="utf-8") == yaml_text


async def test_set_encryption_key_matches_by_mac_fallback(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """A renamed device is still found through the MAC when the name misses."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    device = _configure(ctrl, tmp_path, API_KEY_YAML, mac_address="AA:BB:CC:DD:EE:FF")
    ctrl._scanner._devices_by_name.clear()

    result = await ctrl.set_encryption_key(name=device.name, key=KEY, mac="aabbccddeeff")

    assert result["result"] == "updated"
    assert f'key: "{KEY}"' in (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")


async def test_set_encryption_key_mac_disambiguates_duplicate_names(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """A duplicate-name bucket is narrowed to the device broadcasting the pushed MAC."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    (tmp_path / "kitchen.yaml").write_text(API_KEY_YAML, encoding="utf-8")
    (tmp_path / "kitchen (1).yaml").write_text(API_KEY_YAML, encoding="utf-8")
    right = make_device("kitchen", mac_address="AA:BB:CC:DD:EE:FF")
    wrong = make_device(
        "kitchen", configuration="kitchen (1).yaml", mac_address="11:22:33:44:55:66"
    )
    ctrl._scanner._devices_by_name["kitchen"] = [right, wrong]
    ctrl._scanner.devices = [right, wrong]

    result = await ctrl.set_encryption_key(name="kitchen", key=KEY, mac="aabbccddeeff")

    assert result == {"result": "updated", "configurations": ["kitchen.yaml"]}
    assert KEY in (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert KEY not in (tmp_path / "kitchen (1).yaml").read_text(encoding="utf-8")


async def test_set_encryption_key_stores_pending_for_unadopted_device(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """No configured match → the key lands in the 0600 pending store and survives a reload."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)

    result = await ctrl.set_encryption_key(name="cast-1-w-a1b2c3", key=KEY, mac="A1:B2:C3:D4:E5:F6")

    assert result == {"result": "stored"}
    await drain_shutdown_callbacks(ctrl._shutdown_callbacks)
    store_path = tmp_path / ".device-builder-pending-keys.json"
    assert store_path.is_file()
    if sys.platform != "win32":
        assert (store_path.stat().st_mode & 0o777) == 0o600

    reloaded = PendingKeysStore(data_dir=tmp_path, shutdown_register=lambda cb: None)
    await reloaded.async_load()
    assert reloaded.get("cast-1-w-a1b2c3") == {"key": KEY, "mac": "A1:B2:C3:D4:E5:F6"}


async def test_set_encryption_key_drops_pending_once_configured(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """A push for a now-configured name clears any stale pending entry."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    ctrl._pending_keys.set("kitchen", OTHER_KEY)
    _configure(ctrl, tmp_path, API_KEY_YAML)

    await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert ctrl._pending_keys.get("kitchen") is None


async def test_set_encryption_key_rejects_non_base64_key(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    ctrl = make_controller(tmp_path, with_state_monitor=True)

    with pytest.raises(CommandError) as excinfo:
        await ctrl.set_encryption_key(name="kitchen", key="not base64!!")

    assert excinfo.value.code == ErrorCode.INVALID_ARGS


async def test_set_encryption_key_rejects_wrong_length_key(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    ctrl = make_controller(tmp_path, with_state_monitor=True)

    with pytest.raises(CommandError) as excinfo:
        await ctrl.set_encryption_key(name="kitchen", key=base64.b64encode(b"short").decode())

    assert excinfo.value.code == ErrorCode.INVALID_ARGS


async def test_set_encryption_key_validation_failure_leaves_file_untouched(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """A validator rejection surfaces as CommandError with no disk write."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _configure(ctrl, tmp_path, API_KEY_YAML)
    ctrl._db.editor.validate_yaml = AsyncMock(
        return_value={"yaml_errors": [{"message": "boom"}], "validation_errors": []}
    )

    with pytest.raises(CommandError):
        await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert OTHER_KEY in (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")


async def test_set_encryption_key_refuses_flow_style_api_block(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """A flow-style ``api: {...}`` the line walker can't edit refuses cleanly."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    yaml_text = "esphome:\n  name: kitchen\n\napi: {reboot_timeout: 0s}\n"
    _configure(ctrl, tmp_path, yaml_text, api_enabled=True)

    result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result["result"] == "not_writable"
    assert "flow-style" in result["reason"] or "inline value" in result["reason"]
    assert (tmp_path / "kitchen.yaml").read_text(encoding="utf-8") == yaml_text


async def test_set_encryption_key_round_trip_guard_raises_internal_error(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rewrite the reader can't read back never lands on disk."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _configure(ctrl, tmp_path, API_KEY_YAML)
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.encryption_key.upsert_api_encryption_key",
        lambda content, key: content + "# garbage\n",
    )

    with pytest.raises(CommandError) as excinfo:
        await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert excinfo.value.code == ErrorCode.INTERNAL_ERROR
    assert OTHER_KEY in (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")


async def test_pending_keys_store_decode_tolerates_bad_disk_state(tmp_path: Path) -> None:
    """Corrupt JSON, non-dict roots, and non-dict entries all load as empty/filtered."""
    store_path = tmp_path / ".device-builder-pending-keys.json"
    for raw, expected in (
        (b"{not json", None),
        (b'["list"]', None),
        (b'{"good": {"key": "K=="}, "bad": "str", "nokey": {"mac": "x"}}', {"key": "K=="}),
        (b'{"good": {"key": "K=="}, "bad": {"key": 5}, "empty": {"key": ""}}', {"key": "K=="}),
    ):
        store_path.write_bytes(raw)
        store = PendingKeysStore(data_dir=tmp_path, shutdown_register=lambda cb: None)
        await store.async_load()
        assert store.get("good") == expected
        assert store.get("bad") is None


async def test_pending_keys_store_set_same_entry_skips_save(tmp_path: Path) -> None:
    """Re-pushing the identical entry schedules no redundant write."""
    store = PendingKeysStore(data_dir=tmp_path, shutdown_register=lambda cb: None)
    store.set("a", KEY, "AA:BB:CC:DD:EE:FF")
    saves: list[object] = []
    store._store.async_delay_save = lambda *a, **kw: saves.append(a)  # type: ignore[method-assign]
    store.set("a", KEY, "AA:BB:CC:DD:EE:FF")
    assert saves == []


async def test_pending_keys_store_set_and_pop_roundtrip(tmp_path: Path) -> None:
    """RAM semantics: set overwrites, pop returns and clears, mac optional."""
    store = PendingKeysStore(data_dir=tmp_path, shutdown_register=lambda cb: None)
    store.set("a", KEY)
    assert store.get("a") == {"key": KEY}
    store.set("a", OTHER_KEY, "AA:BB:CC:DD:EE:FF")
    assert store.pop("a") == {"key": OTHER_KEY, "mac": "AA:BB:CC:DD:EE:FF"}
    assert store.pop("a") is None
