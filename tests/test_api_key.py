"""Tests for the Native API encryption-key extraction + scanner flag.

Covers the helper layer (resolves through ESPHome's YAML loader so
``!secret`` / ``!include`` / packages all work) and the scan-time
``Device.api_encrypted`` flag that drives the dashboard's lock-icon
indicator.
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path
from unittest import mock

import pytest
from esphome.components import packages as _real_esphome_packages

from esphome_device_builder.helpers import device_yaml
from esphome_device_builder.helpers.device_yaml import (
    config_has_top_level_block,
    detect_platform_from_yaml,
    get_api_encryption_block,
    get_api_encryption_key,
    load_device_yaml,
)
from esphome_device_builder.models import Device

# ---------------------------------------------------------------------------
# Pure-helper paths — no disk
# ---------------------------------------------------------------------------


def test_get_api_encryption_block_returns_inner_dict() -> None:
    """An ``api: encryption: ...`` block is returned as a dict for the caller to inspect."""
    config = {"api": {"encryption": {"key": "abc=="}}}
    assert get_api_encryption_block(config) == {"key": "abc=="}


def test_get_api_encryption_block_none_when_no_api() -> None:
    assert get_api_encryption_block({"esphome": {"name": "x"}}) is None


def test_get_api_encryption_block_none_when_api_unencrypted() -> None:
    """Bare ``api:`` (Native API enabled but no encryption) → no block."""
    assert get_api_encryption_block({"api": {}}) is None


def test_get_api_encryption_block_handles_non_dict_inputs() -> None:
    """Bad config shapes (None, list, str) don't blow up the helper."""
    assert get_api_encryption_block(None) is None
    assert get_api_encryption_block({"api": "not-a-dict"}) is None
    assert get_api_encryption_block({"api": {"encryption": "not-a-dict"}}) is None


def test_get_api_encryption_key_returns_resolved_string() -> None:
    config = {"api": {"encryption": {"key": "ZGFzaA=="}}}
    assert get_api_encryption_key(config) == "ZGFzaA=="


def test_get_api_encryption_key_empty_when_missing() -> None:
    assert get_api_encryption_key({"api": {"encryption": {}}}) == ""
    assert get_api_encryption_key(None) == ""


def test_config_has_top_level_block() -> None:
    """``api`` / ``mqtt`` etc. are detected even with empty / null values."""
    assert config_has_top_level_block({"api": None}, "api") is True
    assert config_has_top_level_block({"mqtt": {"broker": "x"}}, "mqtt") is True
    assert config_has_top_level_block({"esphome": {}}, "api") is False
    assert config_has_top_level_block(None, "api") is False


# ---------------------------------------------------------------------------
# load_device_yaml — exercises ESPHome's loader, so this hits the file system
# ---------------------------------------------------------------------------


@pytest.fixture
def yaml_file(tmp_path: Path) -> Path:
    return tmp_path / "kitchen.yaml"


def test_load_device_yaml_parses_valid_config(yaml_file: Path) -> None:
    yaml_file.write_text(
        "esphome:\n"
        "  name: kitchen\n"
        "api:\n"
        '  encryption:\n    key: "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="\n'
    )
    config = load_device_yaml(yaml_file)
    assert config is not None
    assert get_api_encryption_key(config) == "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


def test_load_device_yaml_returns_none_on_parse_failure(yaml_file: Path) -> None:
    """An invalid draft mid-edit returns ``None`` instead of raising."""
    yaml_file.write_text("api: !\n  bad: [unterminated\n")
    assert load_device_yaml(yaml_file) is None


def test_load_device_yaml_resolves_secrets(tmp_path: Path) -> None:
    """``!secret`` references resolve through the sibling ``secrets.yaml``.

    The regex-on-raw-YAML approach the frontend used to do gave up
    here — backend resolution is the whole reason ``devices/get_api_key``
    exists.
    """
    (tmp_path / "secrets.yaml").write_text("api_key: 'AAAA=='\n")
    yaml_file = tmp_path / "kitchen.yaml"
    yaml_file.write_text(
        "esphome:\n  name: kitchen\napi:\n  encryption:\n    key: !secret api_key\n"
    )
    config = load_device_yaml(yaml_file)
    assert get_api_encryption_key(config) == "AAAA=="


def test_load_device_yaml_merges_packages(tmp_path: Path) -> None:
    """Top-level blocks contributed by ``packages:`` end up flat in the result.

    Repro of #288: a BLE beacon (or any device sharing a common
    package for api / wifi / ota / target-platform) had the
    dashboard report ``api_encrypted=False``, ``target_platform=""``,
    ``loaded_integrations=[]`` because the unmerged config still
    had those keys nested under ``packages:`` instead of at the
    top level. We delegate to ESPHome's own ``do_packages_pass`` +
    ``merge_packages`` (the same two-step the compiler's
    ``validate_config`` runs) so the dashboard sees what the
    compiler sees.
    """
    (tmp_path / "common.yaml").write_text(
        "esp32:\n"
        "  board: esp32dev\n"
        "api:\n"
        '  encryption:\n    key: "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="\n'
        "wifi:\n  ssid: x\n  password: y\n"
    )
    yaml_file = tmp_path / "ble.yaml"
    yaml_file.write_text("esphome:\n  name: ble\npackages:\n  common: !include common.yaml\n")
    config = load_device_yaml(yaml_file)
    assert config is not None
    # ``packages:`` itself is consumed by the merge — top-level
    # keys are now what the user's compiled firmware actually has.
    assert "packages" not in config
    assert "esp32" in config
    assert "api" in config
    assert "wifi" in config
    assert get_api_encryption_key(config) == "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


def test_detect_platform_from_yaml_falls_back_to_resolved_config(
    tmp_path: Path,
) -> None:
    """Platform detection falls through to the resolved config on raw-scan miss.

    The fast path (raw-text scan) survives mid-edit drafts but
    can't see ``esp32:`` blocks pulled in via ``packages:``. The
    slow path (full ``load_device_yaml`` with package merge) is
    only invoked when the raw scan returned empty, so the typical
    no-packages config still pays only the cheap regex.
    """
    (tmp_path / "board.yaml").write_text(
        "esp32:\n  board: esp32dev\n  framework:\n    type: esp-idf\n"
    )
    yaml_file = tmp_path / "ble.yaml"
    yaml_file.write_text("esphome:\n  name: ble\npackages:\n  board: !include board.yaml\n")
    # Raw scan: no top-level ``esp32:`` line, so it returns "".
    # Fallback: load + package-merge → ``esp32`` becomes top-level.
    assert detect_platform_from_yaml(yaml_file) == "esp32"


def test_detect_platform_from_yaml_keeps_raw_scan_for_inline_platform(
    tmp_path: Path,
) -> None:
    """Top-level inline platform key resolves via the raw-scan fast path.

    Pinning this avoids regressing the fast path: a future
    refactor that always loaded the resolved config would parse
    every YAML on every dashboard scan, which is what the cheap
    regex was put in place to avoid.
    """
    yaml_file = tmp_path / "kitchen.yaml"
    yaml_file.write_text("esphome:\n  name: kitchen\nesp8266:\n  board: nodemcuv2\n")
    assert detect_platform_from_yaml(yaml_file) == "esp8266"


def test_detect_platform_from_yaml_skips_load_when_no_packages_block(
    tmp_path: Path,
) -> None:
    """Config without ``packages:`` doesn't pay the load+merge cost.

    Mid-edit drafts and post-compile-only configs frequently omit
    a top-level platform key (the user gets it from
    ``StorageJSON``). Without the ``packages:`` gate, every such
    YAML would trigger a full ESPHome YAML parse on every
    dashboard scan — pure waste because the merge has nothing to
    surface. Spy on ``load_device_yaml`` to confirm we don't call
    it when the raw text has no ``packages:`` block.
    """
    yaml_file = tmp_path / "kitchen.yaml"
    yaml_file.write_text("esphome:\n  name: kitchen\n# platform comes from storage\n")
    with mock.patch(
        "esphome_device_builder.helpers.device_yaml.load_device_yaml",
        wraps=device_yaml.load_device_yaml,
    ) as spy:
        assert detect_platform_from_yaml(yaml_file) == ""
    spy.assert_not_called()


def test_detect_platform_from_yaml_swallows_parser_exceptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mid-edit drafts can't crash the platform detector.

    ``parse_platform_from_yaml`` is regex-based and fairly
    forgiving, but a future tightening that started raising on
    weird input shouldn't blank the dashboard's platform flag.
    Stub the parser to raise and confirm the caller catches and
    falls through to the empty-string return rather than
    propagating.
    """
    yaml_file = tmp_path / "broken.yaml"
    yaml_file.write_text("esphome:\n  name: x\n")
    monkeypatch.setattr(
        device_yaml,
        "parse_platform_from_yaml",
        mock.MagicMock(side_effect=ValueError("simulated parser failure")),
    )
    assert detect_platform_from_yaml(yaml_file) == ""


def test_detect_platform_from_yaml_returns_empty_when_resolved_config_has_no_platform(
    tmp_path: Path,
) -> None:
    """``packages:`` present but the merged config has no platform key.

    Pins the final ``return ""`` after the resolved-config walk —
    the load fired (raw scan missed AND ``packages:`` was in the
    raw text), the merge succeeded, but no platform key landed at
    the top level. Realistic shape: a `packages:` reference that
    contributes only api / wifi / sensor blocks, with the
    platform expected to come from ``StorageJSON`` post-compile.
    """
    (tmp_path / "common.yaml").write_text(
        "wifi:\n  ssid: x\n  password: y\n"  # no platform key here
    )
    yaml_file = tmp_path / "ble.yaml"
    yaml_file.write_text("esphome:\n  name: ble\npackages:\n  common: !include common.yaml\n")
    assert detect_platform_from_yaml(yaml_file) == ""


def test_load_device_yaml_uses_two_step_when_resolve_packages_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two-step fallback runs when the upstream ``resolve_packages`` is missing.

    Pins the fallback path independently of which esphome release
    happens to be installed in the test runner. CI runs against
    whatever esphome ships today (no ``resolve_packages``); local
    development can hit either side. The forced-None monkeypatch
    makes coverage of the two-step branch deterministic regardless.
    """
    monkeypatch.setattr(device_yaml, "_resolve_packages", None)
    (tmp_path / "common.yaml").write_text(
        "esp32:\n  board: esp32dev\nwifi:\n  ssid: x\n  password: y\n"
    )
    yaml_file = tmp_path / "ble.yaml"
    yaml_file.write_text("esphome:\n  name: ble\npackages:\n  common: !include common.yaml\n")
    config = load_device_yaml(yaml_file)
    assert config is not None
    # Two-step path fired → packages merged → top-level keys
    # surface.
    assert "packages" not in config
    assert "esp32" in config
    assert "wifi" in config


def test_load_device_yaml_uses_upstream_resolve_packages_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the upstream ``resolve_packages`` import is available it wins.

    The two-step ``do_packages_pass`` + ``merge_packages`` path is
    the fallback for older esphome releases — once the upstream PR
    (esphome/esphome#16235) lands and the dashboard's dep floor
    moves past it, the single-call seam takes over. Stubs the
    upstream symbol with a spy and confirms the wrapper goes
    through it (and the two-step is NOT called).
    """
    yaml_file = tmp_path / "with_pkg.yaml"
    yaml_file.write_text("esphome:\n  name: x\npackages:\n  shared:\n    wifi:\n      ssid: y\n")
    spy = mock.MagicMock(side_effect=lambda c: c)
    monkeypatch.setattr(device_yaml, "_resolve_packages", spy)
    with mock.patch.object(device_yaml, "_do_packages_pass") as two_step_spy:
        config = load_device_yaml(yaml_file)
    assert config is not None
    spy.assert_called_once()
    two_step_spy.assert_not_called()


def test_load_device_yaml_recovers_when_merge_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad / unreachable package can't blank the device's metadata.

    Pinning the catch-all error handler on the merge call: if
    ``do_packages_pass`` (or ``resolve_packages``) raises — the
    typical case is a remote package whose git ref vanished, but
    also a malformed local package YAML, a missing file, etc. —
    the function returns the unmerged config so the raw-YAML
    fallback paths at the call sites still surface what they
    can. Pre-fix degradation, not a hard failure.
    """
    yaml_file = tmp_path / "broken_pkg.yaml"
    yaml_file.write_text("esphome:\n  name: x\npackages:\n  shared:\n    wifi:\n      ssid: y\n")
    boom = mock.MagicMock(side_effect=RuntimeError("simulated package failure"))
    monkeypatch.setattr(device_yaml, "_resolve_packages", boom)
    config = load_device_yaml(yaml_file)
    assert config is not None
    # Merge raised → caller keeps the unmerged shape rather than
    # crashing or returning ``None``.
    assert "packages" in config


def test_module_import_handles_missing_resolve_packages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Module load survives an esphome that lacks ``resolve_packages``.

    Pins the ``except ImportError: _resolve_packages = None`` branch
    at module load. Reloads ``device_yaml`` against a stubbed
    ``esphome.components.packages`` that has only the two-step
    helpers — the typical shape of ESPHome releases that ship
    BEFORE esphome/esphome#16235 lands. Without the import guard
    the module would fail to import on those releases.
    """
    real_packages = _real_esphome_packages
    stub = types.SimpleNamespace(
        do_packages_pass=real_packages.do_packages_pass,
        merge_packages=real_packages.merge_packages,
        # Intentionally NO ``resolve_packages`` attribute — that's
        # the upstream-not-yet-shipped state.
    )
    monkeypatch.setitem(sys.modules, "esphome.components.packages", stub)
    reloaded = importlib.reload(device_yaml)
    try:
        assert reloaded._resolve_packages is None
        assert reloaded._do_packages_pass is real_packages.do_packages_pass
        assert reloaded._merge_packages is real_packages.merge_packages
    finally:
        # Restore so subsequent tests see the real module.
        monkeypatch.setitem(sys.modules, "esphome.components.packages", real_packages)
        importlib.reload(device_yaml)


def test_module_import_handles_missing_two_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Module load survives an esphome that drops the two-step helpers.

    Pins the ``except ImportError`` branch on the
    ``do_packages_pass`` / ``merge_packages`` import. Belt-and-
    suspenders for the day esphome ships only ``resolve_packages``
    and removes / renames the two-step. Without the guard the
    module would fail to import once the dep floor moves.
    """
    real_packages = _real_esphome_packages
    stub = types.SimpleNamespace(
        # Only ``resolve_packages`` exposed — no two-step helpers.
        resolve_packages=getattr(real_packages, "resolve_packages", lambda c: c),
    )
    monkeypatch.setitem(sys.modules, "esphome.components.packages", stub)
    reloaded = importlib.reload(device_yaml)
    try:
        assert reloaded._do_packages_pass is None
        assert reloaded._merge_packages is None
        assert reloaded._resolve_packages is stub.resolve_packages
    finally:
        monkeypatch.setitem(sys.modules, "esphome.components.packages", real_packages)
        importlib.reload(device_yaml)


def test_load_device_yaml_falls_back_when_both_imports_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No-op gracefully when neither upstream import shape is available.

    A future esphome that deprecates ``do_packages_pass`` /
    ``merge_packages`` AND moves ``resolve_packages`` (rename,
    refactor, …) would otherwise leave us with no merge path. The
    module's ``try/except ImportError`` guards both imports - the
    function then degrades to the unmerged shape, the same fallback
    we use when a package merge fails at runtime. Pre-fix
    behaviour stays available even if the upstream API surface
    drifts.
    """
    monkeypatch.setattr(device_yaml, "_resolve_packages", None)
    monkeypatch.setattr(device_yaml, "_do_packages_pass", None)
    monkeypatch.setattr(device_yaml, "_merge_packages", None)
    yaml_file = tmp_path / "with_pkg.yaml"
    yaml_file.write_text("esphome:\n  name: x\npackages:\n  shared:\n    wifi:\n      ssid: y\n")
    config = load_device_yaml(yaml_file)
    assert config is not None
    # Without a merge path the ``packages:`` block stays — caller
    # then falls back to the raw-scan / StorageJSON surfaces the
    # rest of the metadata pipeline already handles.
    assert "packages" in config


# ---------------------------------------------------------------------------
# Scan-time integration — load_device_from_storage drives the Device flags
# the frontend reads to render the lock indicator.
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect ``ext_storage_path`` into ``tmp_path`` and bypass StorageJSON.

    ``load_device_from_storage`` walks ``CORE.config_path`` for the
    StorageJSON sidecar, which isn't set in unit tests. Point the helper
    at the temporary directory and force ``StorageJSON.load`` to return
    ``None`` so each test exercises the YAML + flag plumbing only.
    """
    monkeypatch.setattr(
        device_yaml,
        "resolve_storage_path",
        lambda config: tmp_path / f"{config}.json",
    )
    monkeypatch.setattr(device_yaml.StorageJSON, "load", staticmethod(lambda _p: None))
    return tmp_path


def _scan(yaml_path: Path, content: str) -> Device:
    """Write *content* to *yaml_path* and run it through the scanner helper."""
    yaml_path.write_text(content)
    return device_yaml.load_device_from_storage(yaml_path)


def test_load_device_from_storage_sets_api_encrypted_from_resolved_yaml(
    isolated_storage: Path,
) -> None:
    """Scanner output's ``api_encrypted`` reflects the resolved config."""
    device = _scan(
        isolated_storage / "kitchen.yaml",
        'esphome:\n  name: kitchen\napi:\n  encryption:\n    key: "ZGFzaA=="\n',
    )
    assert device.api_enabled is True
    assert device.api_encrypted is True


def test_load_device_from_storage_api_disabled_for_mqtt_only(
    isolated_storage: Path,
) -> None:
    """A device with no ``api:`` block reports neither flag — drives the no-lock case."""
    device = _scan(
        isolated_storage / "sensor.yaml",
        "esphome:\n  name: sensor\nmqtt:\n  broker: 192.168.1.10\n",
    )
    assert device.api_enabled is False
    assert device.api_encrypted is False
    assert device.uses_mqtt is True


def test_load_device_from_storage_falls_back_for_invalid_draft(
    isolated_storage: Path,
) -> None:
    """Mid-edit drafts where ``yaml_util.load_yaml`` fails still get usable flags.

    The lock indicator would otherwise blink off the moment the user
    typed a syntax error. Raw-text fallback keeps the signal stable.
    """
    # Top-level ``api:`` with ``encryption:``, plus a deliberate syntax
    # error further down so ``yaml_util.load_yaml`` returns ``None`` and
    # we fall through to the raw-text heuristic.
    device = _scan(
        isolated_storage / "broken.yaml",
        "esphome:\n  name: broken\n"
        'api:\n  encryption:\n    key: "ZGFzaA=="\n'
        "sensor:\n  - platform: !\n    bad: [unterminated\n",
    )
    assert device.api_enabled is True
    assert device.api_encrypted is True
