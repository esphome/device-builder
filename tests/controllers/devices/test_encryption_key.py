"""Tests for the HA-provisioned encryption-key handoff (``set_encryption_key``)."""

from __future__ import annotations

import asyncio
import base64
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from esphome import espota2

from esphome_device_builder.controllers._device_scanner import ScanChange
from esphome_device_builder.controllers.devices._pending_keys_store import PendingKeysStore
from esphome_device_builder.controllers.devices.encryption_key import _locate_and_stat
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.device_yaml import (
    EsphomeConfigUnavailableError,
    extract_config_content_fingerprint,
)
from esphome_device_builder.helpers.storage import drain_shutdown_callbacks
from esphome_device_builder.helpers.yaml import OldKeyOutcome
from esphome_device_builder.models import ErrorCode
from tests.conftest import make_device

from .conftest import ESPHOME_CONFIG_STUB_TARGET, VALIDATOR_OUTAGES, MakeControllerFactory

KEY = base64.b64encode(b"k" * 32).decode()
OTHER_KEY = base64.b64encode(b"j" * 32).decode()
THIRD_KEY = base64.b64encode(b"h" * 32).decode()
BARE_OTA_YAML = "\nota:\n  - platform: esphome\n    encryption:\n"
OLD_KEY_LINE = f'      old_key: "{OTHER_KEY}"\n'

API_KEY_YAML = f"""\
esphome:
  name: kitchen

api:
  encryption:
    key: "{OTHER_KEY}"

wifi:
  ssid: x
"""


@pytest.fixture
def legacy_esphome(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(espota2, "CONF_OLD_KEY", raising=False)


@pytest.fixture
def old_key_esphome(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(espota2, "CONF_OLD_KEY", "old_key", raising=False)


def _stamp_regen(ctrl, config_hash: str, fingerprint: str) -> None:
    """Stamp a storage regen result for kitchen.yaml."""
    ctrl._metadata_store.update(
        "kitchen.yaml", regen_config_hash=config_hash, regen_content_fingerprint=fingerprint
    )


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


async def test_set_encryption_key_keeps_the_key_when_the_file_changed_during_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_controller: MakeControllerFactory
) -> None:
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _configure(ctrl, tmp_path, API_KEY_YAML)
    concurrent = API_KEY_YAML + "logger:\n"

    async def _save_lands_meanwhile(*_args: object, **_kwargs: object) -> SimpleNamespace:
        await ctrl.update_config(configuration="kitchen.yaml", content=concurrent)
        return SimpleNamespace(unavailable=False)

    monkeypatch.setattr(ctrl, "_validate_rewritten_yaml_or_raise", _save_lands_meanwhile)

    result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result["result"] == "not_writable"
    assert "differs from the expected text" in result["reason"]
    assert "\n" not in result["reason"]
    assert ":;" not in result["reason"]
    assert result["reason"].endswith("the key was kept for a later attempt")
    assert (tmp_path / "kitchen.yaml").read_text(encoding="utf-8") == concurrent
    assert ctrl._pending_keys.get("kitchen") == {"key": KEY}


async def test_set_encryption_key_keeps_the_key_when_the_config_vanished(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _configure(ctrl, tmp_path, API_KEY_YAML)
    await asyncio.to_thread((tmp_path / "kitchen.yaml").unlink)

    result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result["result"] == "not_writable"
    assert result["reason"] == "Device 'kitchen.yaml' not found"
    assert ctrl._pending_keys.get("kitchen") == {"key": KEY}


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
    assert new_yaml == f'{yaml_text}\napi:\n  encryption:\n    key: "{KEY}"\n'


async def test_set_encryption_key_refuses_resolved_apiless_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """The resolved config genuinely lacks ``api:`` → refuse, don't re-enable it."""
    resolve = AsyncMock(return_value={"esphome": {"name": "kitchen"}, "mqtt": {}})
    monkeypatch.setattr(ESPHOME_CONFIG_STUB_TARGET, resolve)
    ctrl = make_controller(tmp_path, with_state_monitor=True, esphome_cmd=["esphome"])
    yaml_text = "esphome:\n  name: kitchen\n\nmqtt:\n  broker: b\n"
    _configure(ctrl, tmp_path, yaml_text, loaded_integrations=["mqtt", "wifi"], api_enabled=False)

    result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result["result"] == "not_writable"
    assert "does not enable the native API" in result["reason"]
    assert (tmp_path / "kitchen.yaml").read_text(encoding="utf-8") == yaml_text
    # The only copy of the key is kept for a later push / readopt.
    assert ctrl._pending_keys.get("kitchen") == {"key": KEY}


SECRET_KEY_YAML = "esphome:\n  name: kitchen\n\napi:\n  encryption:\n    key: !secret api_key\n"
SUBSTITUTED_KEY_YAML = (
    f'substitutions:\n  api_key: "{KEY}"\n\nesphome:\n  name: kitchen\n\n'
    "api:\n  encryption:\n    key: ${api_key}\n"
)


@pytest.mark.parametrize(
    ("yaml_text", "secret", "expected", "fragment"),
    [
        pytest.param(SECRET_KEY_YAML, KEY, "unchanged", "", id="secret_matches"),
        pytest.param(SUBSTITUTED_KEY_YAML, None, "unchanged", "", id="substitution_matches"),
        pytest.param(SECRET_KEY_YAML, OTHER_KEY, "not_writable", "different value", id="differs"),
        pytest.param(SECRET_KEY_YAML, None, "not_writable", "could not be resolved", id="missing"),
    ],
)
async def test_set_encryption_key_indirected_key_is_resolved_never_rewritten(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    yaml_text: str,
    secret: str | None,
    expected: str,
    fragment: str,
) -> None:
    """An indirected key settles only by resolving to the pushed key; the file is never touched."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    if secret is not None:
        (tmp_path / "secrets.yaml").write_text(f'api_key: "{secret}"\n', encoding="utf-8")
    _configure(ctrl, tmp_path, yaml_text)
    ctrl._pending_keys.set("kitchen", KEY, "")

    result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result["result"] == expected
    assert (tmp_path / "kitchen.yaml").read_text(encoding="utf-8") == yaml_text
    if expected == "unchanged":
        assert "reason" not in result
        assert ctrl._pending_keys.get("kitchen") is None
    else:
        assert "!secret" in result["reason"]
        assert fragment in result["reason"]
        assert ctrl._pending_keys.get("kitchen") == {"key": KEY}


OTA_KEY_YAML = f"""\
esphome:
  name: kitchen

api:
  encryption:
    key: "{OTHER_KEY}"

ota:
  - platform: esphome
    encryption:
      key: "{OTHER_KEY}"
"""


async def test_set_encryption_key_matching_secret_next_to_a_differing_ota_key_is_refused(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """A differing literal OTA key next to a matching secret is refused, nothing written."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    (tmp_path / "secrets.yaml").write_text(f'api_key: "{KEY}"\n', encoding="utf-8")
    yaml_text = OTA_KEY_YAML.replace(f'key: "{OTHER_KEY}"', "key: !secret api_key", 1)
    _configure(ctrl, tmp_path, yaml_text)

    result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result["result"] == "not_writable"
    assert "OTA encryption key differs" in result["reason"]
    assert (tmp_path / "kitchen.yaml").read_text(encoding="utf-8") == yaml_text


async def test_set_encryption_key_matching_secret_next_to_a_package_ota_key_is_refused(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """An OTA key the raw file can't show (package-merged) is still compared after resolving."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    (tmp_path / "secrets.yaml").write_text(f'api_key: "{KEY}"\n', encoding="utf-8")
    yaml_text = SECRET_KEY_YAML + (
        "\npackages:\n  ota_pkg:\n    ota:\n      - platform: esphome\n"
        f'        encryption:\n          key: "{OTHER_KEY}"\n'
    )
    _configure(ctrl, tmp_path, yaml_text)

    result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result["result"] == "not_writable"
    assert "OTA encryption key differs" in result["reason"]
    assert (tmp_path / "kitchen.yaml").read_text(encoding="utf-8") == yaml_text


@pytest.mark.parametrize(
    ("ota_secret", "expected"),
    [
        pytest.param(KEY, "unchanged", id="same"),
        pytest.param(OTHER_KEY, "not_writable", id="differs"),
    ],
)
async def test_set_encryption_key_indirected_ota_key_is_resolved_next_to_a_matching_secret(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    ota_secret: str,
    expected: str,
) -> None:
    """An indirected OTA key is compared after resolving, not deferred to esphome."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    (tmp_path / "secrets.yaml").write_text(
        f'api_key: "{KEY}"\nota_key: "{ota_secret}"\n', encoding="utf-8"
    )
    yaml_text = OTA_KEY_YAML.replace(f'key: "{OTHER_KEY}"', "key: !secret api_key", 1)
    yaml_text = yaml_text.replace(f'key: "{OTHER_KEY}"', "key: !secret ota_key", 1)
    _configure(ctrl, tmp_path, yaml_text)

    result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result["result"] == expected
    assert (tmp_path / "kitchen.yaml").read_text(encoding="utf-8") == yaml_text


async def test_set_encryption_key_matching_secret_next_to_an_empty_ota_key_is_unchanged(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """An empty OTA ``key:`` inherits the api key, so nothing competes and nothing is written."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    (tmp_path / "secrets.yaml").write_text(f'api_key: "{KEY}"\n', encoding="utf-8")
    yaml_text = OTA_KEY_YAML.replace(f'key: "{OTHER_KEY}"', "key: !secret api_key", 1)
    yaml_text = yaml_text.replace(f'key: "{OTHER_KEY}"', "key:", 1)
    _configure(ctrl, tmp_path, yaml_text)

    result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result == {"result": "unchanged", "configurations": ["kitchen.yaml"]}
    assert (tmp_path / "kitchen.yaml").read_text(encoding="utf-8") == yaml_text


@pytest.mark.usefixtures("legacy_esphome")
async def test_set_encryption_key_collapses_explicit_ota_key_to_a_bare_block(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """An explicit ota key becomes a bare ``encryption:`` that inherits the pushed api key."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _configure(ctrl, tmp_path, OTA_KEY_YAML)

    result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result["result"] == "updated"
    assert "esphome 2026.11 or newer" in result["reason"]
    new_yaml = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert new_yaml.count(f'key: "{KEY}"') == 1
    assert new_yaml.endswith("  - platform: esphome\n    encryption:\n")
    assert OTHER_KEY not in new_yaml


async def test_set_encryption_key_drops_stale_ota_key_when_api_already_matches(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """A stale ota key is dropped even when the api key already carries the push."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _configure(ctrl, tmp_path, OTA_KEY_YAML.replace(f'key: "{OTHER_KEY}"', f'key: "{KEY}"', 1))

    result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result["result"] == "updated"
    new_yaml = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert new_yaml.count(f'key: "{KEY}"') == 1
    assert new_yaml.endswith("    encryption:\n")
    assert OTHER_KEY not in new_yaml


async def test_set_encryption_key_unchanged_when_both_keys_match(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Matching api and ota keys report ``unchanged`` with no write."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    yaml_text = OTA_KEY_YAML.replace(OTHER_KEY, KEY)
    _configure(ctrl, tmp_path, yaml_text)

    result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result == {"result": "unchanged", "configurations": ["kitchen.yaml"]}
    assert (tmp_path / "kitchen.yaml").read_text(encoding="utf-8") == yaml_text


async def test_set_encryption_key_never_overwrites_a_devices_own_ota_key(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """A runtime api key next to the OTA platform's own key is refused; that key stays."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    yaml_text = OTA_KEY_YAML.replace(f'    key: "{OTHER_KEY}"\n', "", 1)
    _configure(ctrl, tmp_path, yaml_text)

    result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result["result"] == "not_writable"
    assert "own encryption key" in result["reason"]
    assert (tmp_path / "kitchen.yaml").read_text(encoding="utf-8") == yaml_text
    assert ctrl._pending_keys.get("kitchen") == {"key": KEY}


@pytest.mark.usefixtures("legacy_esphome")
async def test_set_encryption_key_bare_ota_encryption_is_left_alone(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """A bare ``ota: encryption:`` inherits the api key and needs no rewrite."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _configure(ctrl, tmp_path, API_KEY_YAML + BARE_OTA_YAML)

    result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result["result"] == "updated"
    new_yaml = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert new_yaml.count(KEY) == 1
    assert new_yaml.endswith("    encryption:\n")


@pytest.mark.usefixtures("old_key_esphome")
@pytest.mark.parametrize(
    "yaml_text",
    [
        pytest.param(OTA_KEY_YAML, id="keyed-block"),
        pytest.param(API_KEY_YAML + BARE_OTA_YAML, id="bare-block"),
    ],
)
async def test_set_encryption_key_records_previous_key_as_old_key(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    yaml_text: str,
) -> None:
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _configure(ctrl, tmp_path, yaml_text)

    result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result == {"result": "updated", "configurations": ["kitchen.yaml"]}
    new_yaml = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert new_yaml.count(f'key: "{KEY}"') == 1
    assert new_yaml.endswith("  - platform: esphome\n    encryption:\n" + OLD_KEY_LINE)
    assert new_yaml.count(OTHER_KEY) == 1


@pytest.mark.usefixtures("old_key_esphome")
async def test_set_encryption_key_without_an_encryption_block_writes_no_old_key(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _configure(ctrl, tmp_path, API_KEY_YAML + "\nota:\n  - platform: esphome\n")

    result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result["result"] == "updated"
    new_yaml = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert "old_key" not in new_yaml
    assert OTHER_KEY not in new_yaml


@pytest.mark.usefixtures("old_key_esphome")
async def test_set_encryption_key_re_push_after_old_key_is_unchanged(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    yaml_text = API_KEY_YAML.replace(OTHER_KEY, KEY) + BARE_OTA_YAML + OLD_KEY_LINE
    _configure(ctrl, tmp_path, yaml_text)

    result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result == {"result": "unchanged", "configurations": ["kitchen.yaml"]}
    assert (tmp_path / "kitchen.yaml").read_text(encoding="utf-8") == yaml_text


@pytest.mark.usefixtures("old_key_esphome")
@pytest.mark.parametrize(
    ("deployed_hash", "stamped_fingerprint", "expected_old_key"),
    [
        pytest.param("h1", "current", OTHER_KEY, id="device-runs-the-yaml"),
        pytest.param("h0", "current", THIRD_KEY, id="device-behind-the-yaml"),
        pytest.param("", "current", THIRD_KEY, id="device-hash-unknown"),
        pytest.param("h1", "v1:stale", THIRD_KEY, id="regen-read-older-content"),
        pytest.param("h1", "", THIRD_KEY, id="no-regen-stamp"),
    ],
)
async def test_set_encryption_key_second_rekey_replaces_old_key_only_when_the_device_runs_the_yaml(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    caplog: pytest.LogCaptureFixture,
    deployed_hash: str,
    stamped_fingerprint: str,
    expected_old_key: str,
) -> None:
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    yaml_text = API_KEY_YAML + BARE_OTA_YAML + f'      old_key: "{THIRD_KEY}"\n'
    _configure(
        ctrl, tmp_path, yaml_text, deployed_config_hash=deployed_hash, ota_encryption_required=True
    )
    if stamped_fingerprint == "current":
        stamped_fingerprint = extract_config_content_fingerprint(yaml_text)
    if stamped_fingerprint:
        _stamp_regen(ctrl, "h1", stamped_fingerprint)

    with caplog.at_level(logging.WARNING):
        result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result["result"] == "updated"
    new_yaml = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert new_yaml.count(f'key: "{KEY}"') == 1
    assert new_yaml.endswith(f'    encryption:\n      old_key: "{expected_old_key}"\n')
    assert new_yaml.count(OTHER_KEY) + new_yaml.count(THIRD_KEY) == 1
    kept = expected_old_key == THIRD_KEY
    assert ("an earlier old_key was kept" in caplog.text) is kept
    assert ("reason" in result) is kept
    assert OTHER_KEY not in caplog.text


@pytest.mark.usefixtures("old_key_esphome")
async def test_set_encryption_key_indirected_old_key_is_left_as_is_and_named(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    yaml_text = API_KEY_YAML + BARE_OTA_YAML + "      old_key: !secret previous_key\n"
    # The scanned flag is stale on purpose: the old_key in the file is proof enough.
    _configure(ctrl, tmp_path, yaml_text, deployed_config_hash="h1")
    _stamp_regen(ctrl, "h1", extract_config_content_fingerprint(yaml_text))

    with caplog.at_level(logging.WARNING):
        result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result["result"] == "updated"
    assert "via !secret or a substitution" in result["reason"]
    new_yaml = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert new_yaml.endswith("    encryption:\n      old_key: !secret previous_key\n")
    assert OTHER_KEY not in new_yaml
    assert "an earlier old_key was kept" not in caplog.text


@pytest.mark.usefixtures("old_key_esphome")
async def test_set_encryption_key_drops_old_key_equal_to_pushed_key(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    yaml_text = OTA_KEY_YAML.replace(OTHER_KEY, KEY, 1) + f'      old_key: "{KEY}"\n'
    _configure(ctrl, tmp_path, yaml_text)

    result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result["result"] == "updated"
    new_yaml = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert new_yaml.count(KEY) == 1
    assert new_yaml.endswith("    encryption:\n")


@pytest.mark.usefixtures("legacy_esphome")
async def test_set_encryption_key_legacy_esphome_skips_old_key_and_warns(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _configure(ctrl, tmp_path, OTA_KEY_YAML, ota_encryption_required=True)

    with caplog.at_level(logging.WARNING):
        result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result["result"] == "updated"
    assert "esphome 2026.11 or newer" in result["reason"]
    new_yaml = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert "old_key" not in new_yaml
    assert new_yaml.endswith("    encryption:\n")
    assert "esphome 2026.11 or newer" in caplog.text
    assert OTHER_KEY not in caplog.text


@pytest.mark.usefixtures("old_key_esphome")
@pytest.mark.parametrize(
    ("ota_text", "required", "expected"),
    [
        pytest.param("", True, "is not in this file", id="packaged-ota"),
        pytest.param(
            "\nota: !include common/ota.yaml\n", True, "no encryption: block", id="included"
        ),
        pytest.param(
            "\nota:\n  - platform: esphome\n", True, "no encryption: block", id="packaged-enc"
        ),
        pytest.param("\nota: {platform: esphome}\n", True, "no encryption: block", id="flow-style"),
        pytest.param("", False, None, id="no-ota-encryption"),
    ],
)
async def test_set_encryption_key_warns_when_a_required_ota_block_cannot_take_old_key(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    caplog: pytest.LogCaptureFixture,
    ota_text: str,
    required: bool,
    expected: str | None,
) -> None:
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _configure(ctrl, tmp_path, API_KEY_YAML + ota_text, ota_encryption_required=required)

    with caplog.at_level(logging.WARNING):
        result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result["result"] == "updated"
    assert ("reason" in result) is (expected is not None)
    if expected is not None:
        assert expected in result["reason"]
        assert expected in caplog.text
    assert OTHER_KEY not in caplog.text


@pytest.mark.usefixtures("old_key_esphome")
async def test_set_encryption_key_first_key_on_a_required_ota_block_has_nothing_to_record(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    yaml_text = "esphome:\n  name: kitchen\n\napi:\n  encryption:\n" + BARE_OTA_YAML
    _configure(ctrl, tmp_path, yaml_text, api_enabled=True, ota_encryption_required=True)

    with caplog.at_level(logging.WARNING):
        result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result == {"result": "updated", "configurations": ["kitchen.yaml"]}
    new_yaml = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert new_yaml.count(KEY) == 1
    assert "old_key" not in new_yaml
    assert "previous one" not in caplog.text


@pytest.mark.usefixtures("old_key_esphome")
async def test_set_encryption_key_validator_rejecting_old_key_keeps_the_key(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _configure(ctrl, tmp_path, OTA_KEY_YAML)
    ctrl._db.editor.validate_yaml = AsyncMock(
        return_value={
            "yaml_errors": [],
            "validation_errors": [{"message": "[old_key] is an invalid option for [encryption]"}],
        }
    )

    result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result["result"] == "not_writable"
    assert "old_key" in result["reason"]
    assert (tmp_path / "kitchen.yaml").read_text(encoding="utf-8") == OTA_KEY_YAML
    assert ctrl._pending_keys.get("kitchen") == {"key": KEY}


async def test_set_encryption_key_unchanged_with_matching_api_and_indirected_ota_key(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """A secret-backed ota key next to an already matching api key needs no write."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    yaml_text = OTA_KEY_YAML.replace(OTHER_KEY, KEY, 1).replace(
        f'key: "{OTHER_KEY}"', "key: !secret api_key", 1
    )
    _configure(ctrl, tmp_path, yaml_text)

    result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result == {"result": "unchanged", "configurations": ["kitchen.yaml"]}
    assert (tmp_path / "kitchen.yaml").read_text(encoding="utf-8") == yaml_text


async def test_set_encryption_key_drops_an_empty_ota_key_when_api_already_matches(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """An empty explicit ota key is dropped even when the api key already carries the push."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    yaml_text = OTA_KEY_YAML.replace(OTHER_KEY, KEY, 1).replace(f'key: "{OTHER_KEY}"', "key:", 1)
    _configure(ctrl, tmp_path, yaml_text)

    result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result["result"] == "updated"
    new_yaml = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert new_yaml.count(f'key: "{KEY}"') == 1
    assert new_yaml.endswith("    encryption:\n")


async def test_set_encryption_key_refuses_indirected_ota_key(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """An ota ``!secret`` key can't be made to match; refuse and keep the pending copy."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    yaml_text = OTA_KEY_YAML.replace(f'key: "{OTHER_KEY}"\n', "key: !secret ota_key\n")
    yaml_text = yaml_text.replace("key: !secret ota_key", f'key: "{OTHER_KEY}"', 1)
    _configure(ctrl, tmp_path, yaml_text)

    result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result["result"] == "not_writable"
    assert "OTA encryption key" in result["reason"]
    assert (tmp_path / "kitchen.yaml").read_text(encoding="utf-8") == yaml_text
    assert ctrl._pending_keys.get("kitchen") == {"key": KEY}


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


async def test_set_encryption_key_partial_refusal_keeps_reason(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """A refused duplicate-name sibling stays visible next to the aggregate success."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    (tmp_path / "kitchen.yaml").write_text(API_KEY_YAML, encoding="utf-8")
    (tmp_path / "kitchen (1).yaml").write_text(
        "esphome:\n  name: kitchen\n\napi:\n  encryption:\n    key: !secret api_key\n",
        encoding="utf-8",
    )
    writable = make_device("kitchen")
    refused = make_device("kitchen", configuration="kitchen (1).yaml")
    ctrl._scanner._devices_by_name["kitchen"] = [writable, refused]
    ctrl._scanner.devices = [writable, refused]

    result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result["result"] == "updated"
    assert "!secret" in result["reason"]
    # The sibling's update consumed the key, so the refusal must not claim it was kept.
    assert "kept for a later attempt" not in result["reason"]
    assert ctrl._pending_keys.get("kitchen") is None


@pytest.mark.usefixtures("legacy_esphome")
async def test_set_encryption_key_refusal_outranks_an_advisory_on_duplicate_names(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    (tmp_path / "kitchen.yaml").write_text(OTA_KEY_YAML, encoding="utf-8")
    (tmp_path / "kitchen (1).yaml").write_text(
        "esphome:\n  name: kitchen\n\napi:\n  encryption:\n    key: !secret api_key\n",
        encoding="utf-8",
    )
    advised = make_device("kitchen", ota_encryption_required=True)
    refused = make_device("kitchen", configuration="kitchen (1).yaml")
    ctrl._scanner._devices_by_name["kitchen"] = [advised, refused]
    ctrl._scanner.devices = [advised, refused]

    with caplog.at_level(logging.INFO):
        result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result["result"] == "updated"
    assert "!secret" in result["reason"]
    assert "2026.11" not in result["reason"]
    assert "refused: the key is provided via !secret" in caplog.text
    assert "esphome 2026.11 or newer" in caplog.text


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


async def test_set_encryption_key_all_refused_keeps_pending(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """An all-refused push must not destroy the only stored copy of the key."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    ctrl._pending_keys.set("kitchen", KEY)
    yaml_text = "esphome:\n  name: kitchen\n\napi:\n  encryption:\n    key: !secret api_key\n"
    _configure(ctrl, tmp_path, yaml_text)

    result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result["result"] == "not_writable"
    assert ctrl._pending_keys.get("kitchen") == {"key": KEY}


async def test_set_encryption_key_never_compiled_package_device_gets_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A package device with no compile yet still gets the key once resolve confirms api."""
    resolve = AsyncMock(return_value={"api": None, "esphome": {"name": "kitchen"}})
    monkeypatch.setattr(ESPHOME_CONFIG_STUB_TARGET, resolve)
    ctrl = make_controller(tmp_path, with_state_monitor=True, esphome_cmd=["esphome"])
    yaml_text = "substitutions:\n  name: kitchen\n\npackages:\n  v: github://x/y.yaml\n"
    _configure(ctrl, tmp_path, yaml_text, api_enabled=False)
    # The bypass-init _db mock would swallow the post-persist regen
    # task's coroutine; pre-marking pending short-circuits the schedule.
    ctrl.state.regen.pending.add("kitchen.yaml")

    result = await ctrl.set_encryption_key(name="kitchen", key=KEY, mac="aabbccddeeff")

    assert result["result"] == "updated"
    assert f'key: "{KEY}"' in (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")


async def test_set_encryption_key_apiless_verdict_is_kept_until_the_yaml_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A resolved no-``api:`` verdict answers the next push off the file identity, not a spawn."""
    resolve = AsyncMock(return_value={"esphome": {"name": "kitchen"}, "mqtt": {}})
    monkeypatch.setattr(ESPHOME_CONFIG_STUB_TARGET, resolve)
    ctrl = make_controller(tmp_path, with_state_monitor=True, esphome_cmd=["esphome"])
    yaml_text = "substitutions:\n  name: kitchen\n\npackages:\n  v: github://x/y.yaml\n"
    _configure(ctrl, tmp_path, yaml_text, api_enabled=False)

    first = await ctrl.set_encryption_key(name="kitchen", key=KEY)
    second = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert first["result"] == second["result"] == "not_writable"
    assert "does not enable the native API" in second["reason"]
    assert resolve.await_count == 1

    (tmp_path / "kitchen.yaml").write_text(yaml_text + "# edited\n", encoding="utf-8")
    third = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert third["result"] == "not_writable"
    assert resolve.await_count == 2


async def test_set_encryption_key_apiless_verdict_is_dropped_when_the_scanner_sees_a_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A scan UPDATED or REMOVED for the configuration prunes its remembered verdict."""
    resolve = AsyncMock(return_value={"esphome": {"name": "kitchen"}, "mqtt": {}})
    monkeypatch.setattr(ESPHOME_CONFIG_STUB_TARGET, resolve)
    ctrl = make_controller(tmp_path, with_state_monitor=True, esphome_cmd=["esphome"])
    yaml_text = "substitutions:\n  name: kitchen\n\npackages:\n  v: github://x/y.yaml\n"
    device = _configure(ctrl, tmp_path, yaml_text, api_enabled=False)

    await ctrl.set_encryption_key(name="kitchen", key=KEY)
    assert "kitchen.yaml" in ctrl.state.apiless_resolves

    ctrl._on_scan_change(ScanChange.REMOVED, device)

    assert ctrl.state.apiless_resolves == {}


def test_locate_and_stat_has_no_identity_for_a_missing_file(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A YAML that vanished between the read and the stat carries no identity to remember."""
    settings = make_controller(tmp_path)._db.settings
    path, identity = _locate_and_stat(settings, "gone.yaml")
    assert path == tmp_path / "gone.yaml" and identity is None
    (tmp_path / "kitchen.yaml").write_text("esphome:\n", encoding="utf-8")
    assert _locate_and_stat(settings, "kitchen.yaml")[1] is not None


async def test_set_encryption_key_unresolvable_verdict_is_retried_on_the_next_push(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A failed resolve is never remembered; the retained key gets another attempt."""
    resolve = AsyncMock(side_effect=EsphomeConfigUnavailableError("timed out"))
    monkeypatch.setattr(ESPHOME_CONFIG_STUB_TARGET, resolve)
    ctrl = make_controller(tmp_path, with_state_monitor=True, esphome_cmd=["esphome"])
    yaml_text = "substitutions:\n  name: kitchen\n\npackages:\n  v: github://x/y.yaml\n"
    _configure(ctrl, tmp_path, yaml_text, api_enabled=False)

    await ctrl.set_encryption_key(name="kitchen", key=KEY)
    await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert resolve.await_count == 2
    assert ctrl.state.apiless_resolves == {}


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param("unavailable", id="resolve_unavailable"),
        pytest.param("invalid", id="resolve_invalid"),
        pytest.param("no_cli", id="no_esphome_cmd"),
    ],
)
async def test_set_encryption_key_unresolvable_config_keeps_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
    mode: str,
) -> None:
    """An unconfirmable config refuses without guessing and keeps the key."""
    if mode == "unavailable":
        resolve = AsyncMock(side_effect=EsphomeConfigUnavailableError("timed out"))
    else:
        resolve = AsyncMock(return_value=None)
    monkeypatch.setattr(ESPHOME_CONFIG_STUB_TARGET, resolve)
    esphome_cmd = [] if mode == "no_cli" else ["esphome"]
    ctrl = make_controller(tmp_path, with_state_monitor=True, esphome_cmd=esphome_cmd)
    yaml_text = "substitutions:\n  name: kitchen\n\npackages:\n  v: github://x/y.yaml\n"
    _configure(ctrl, tmp_path, yaml_text, api_enabled=False)

    result = await ctrl.set_encryption_key(name="kitchen", key=KEY, mac="aabbccddeeff")

    assert result["result"] == "not_writable"
    assert "could not be resolved" in result["reason"]
    assert ctrl._pending_keys.get("kitchen") == {"key": KEY, "mac": "AA:BB:CC:DD:EE:FF"}
    if mode == "no_cli":
        resolve.assert_not_awaited()


@pytest.mark.parametrize("exc", VALIDATOR_OUTAGES)
async def test_set_encryption_key_validator_outage_is_typed_and_keeps_key(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    exc: Exception,
) -> None:
    """A validator outage refuses cleanly instead of escaping as a 500."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _configure(ctrl, tmp_path, API_KEY_YAML)
    ctrl._db.editor.validate_yaml = AsyncMock(side_effect=exc)

    result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result["result"] == "not_writable"
    assert "validator was unavailable" in result["reason"]
    assert OTHER_KEY in (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert ctrl._pending_keys.get("kitchen") == {"key": KEY}


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

    result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result["result"] == "not_writable"
    assert "boom" in result["reason"]
    assert OTHER_KEY in (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    # The invariant: no refusal shape ever loses the only copy.
    assert ctrl._pending_keys.get("kitchen") == {"key": KEY}


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


async def test_set_encryption_key_round_trip_guard_refuses_and_keeps_key(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rewrite the reader can't read back never lands on disk."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _configure(ctrl, tmp_path, API_KEY_YAML)
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.encryption_key.rotate_api_encryption_key",
        lambda content, key, **_kwargs: (
            content + "# garbage\n",
            OldKeyOutcome.NOT_NEEDED,
        ),
    )

    result = await ctrl.set_encryption_key(name="kitchen", key=KEY)

    assert result["result"] == "not_writable"
    assert "round-trip" in result["reason"]
    assert OTHER_KEY in (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert ctrl._pending_keys.get("kitchen") == {"key": KEY}


async def test_pending_keys_store_decode_tolerates_bad_disk_state(tmp_path: Path) -> None:
    """Corrupt JSON, malformed entries, and non-base64 keys all load as empty/filtered."""
    store_path = tmp_path / ".device-builder-pending-keys.json"
    good = f'"good": {{"key": "{KEY}"}}'
    for raw, expected in (
        (b"{not json", None),
        (b'["list"]', None),
        (f'{{{good}, "bad": "str", "nokey": {{"mac": "x"}}}}'.encode(), {"key": KEY}),
        (f'{{{good}, "bad": {{"key": 5}}, "empty": {{"key": ""}}}}'.encode(), {"key": KEY}),
        (f'{{{good}, "bad": {{"key": "not-base64-32!"}}}}'.encode(), {"key": KEY}),
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


async def test_pending_keys_store_pop_if_keeps_a_newer_key(tmp_path: Path) -> None:
    """``pop_if`` consumes the entry only while it still holds the key that was handled."""
    store = PendingKeysStore(data_dir=tmp_path, shutdown_register=lambda cb: None)
    store.set("kitchen", KEY)
    assert store.pop_if("kitchen", OTHER_KEY) is None
    assert store.get("kitchen") == {"key": KEY}
    assert store.pop_if("kitchen", KEY) == {"key": KEY}
    assert store.pop_if("kitchen", KEY) is None


async def test_pending_keys_store_set_and_pop_roundtrip(tmp_path: Path) -> None:
    """RAM semantics: set overwrites, pop returns and clears, mac optional."""
    store = PendingKeysStore(data_dir=tmp_path, shutdown_register=lambda cb: None)
    store.set("a", KEY)
    assert store.get("a") == {"key": KEY}
    store.set("a", OTHER_KEY, "AA:BB:CC:DD:EE:FF")
    assert store.pop("a") == {"key": OTHER_KEY, "mac": "AA:BB:CC:DD:EE:FF"}
    assert store.pop("a") is None
