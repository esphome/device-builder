"""Tests for the Native API encryption-key extraction + scanner flag.

Covers the helper layer (resolves through ESPHome's YAML loader so
``!secret`` / ``!include`` / packages all work) and the scan-time
``Device.api_encrypted`` flag that drives the dashboard's lock-icon
indicator.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from esphome_device_builder.helpers import device_yaml
from esphome_device_builder.helpers.device_yaml import (
    _has_remote_packages,
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
    ``merge_packages`` (the same two-step the compiler runs at
    ``esphome.config:1010-1039``) so the dashboard sees what the
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


def test_load_device_yaml_skips_merge_for_remote_packages(
    tmp_path: Path,
) -> None:
    """Remote-package configs DON'T trigger the merge — would block on git clone.

    A ``url:`` package fires ``git clone`` synchronously inside
    ``do_packages_pass``; first-run latency is 5-10 minutes on
    slow connections / large repos. The dashboard's metadata
    refresh runs on the WS event loop, so we gate the merge on
    "no remote packages anywhere in the tree". Configs that hit
    this gate degrade to the unmerged shape (same as pre-fix); a
    follow-up will add an executor + mtime cache to support
    remote packages without blocking.

    Spy on ``do_packages_pass`` / ``resolve_packages`` to confirm
    neither runs when a remote package is present.
    """
    yaml_file = tmp_path / "remote.yaml"
    yaml_file.write_text(
        "esphome:\n  name: remote\n"
        "packages:\n"
        "  shared:\n"
        "    url: https://github.com/example/shared\n"
        "    files:\n      - shared.yaml\n"
        "    ref: main\n"
    )
    with (
        mock.patch("esphome_device_builder.helpers.device_yaml._do_packages_pass") as do_pkg,
        mock.patch("esphome_device_builder.helpers.device_yaml._resolve_packages") as resolve_pkg,
    ):
        config = load_device_yaml(yaml_file)
    assert config is not None
    # Merge skipped → ``packages:`` is still in the result.
    assert "packages" in config
    do_pkg.assert_not_called()
    resolve_pkg.assert_not_called()


def test_has_remote_packages_local_only_returns_false() -> None:
    """A purely local ``packages:`` tree (inline dicts) is safe to merge.

    Walks the canonical local-package shapes — inline dict
    fragment, ``IncludeFile`` substitute (any non-``url:`` dict),
    list of inline dicts — and confirms the gate stays open. A
    False positive here would silently disable the merge for the
    BLE-beacon configs from #288 the fix was written for.
    """
    assert _has_remote_packages({"shared": {"wifi": {"ssid": "x"}}}) is False
    assert _has_remote_packages({}) is False
    assert _has_remote_packages({"a": {"foo": 1}, "b": {"bar": 2}}) is False


def test_has_remote_packages_url_dict_returns_true() -> None:
    """A package definition with ``url:`` is remote — gate has to bail."""
    assert (
        _has_remote_packages(
            {"shared": {"url": "https://github.com/example/shared", "ref": "main"}}
        )
        is True
    )


def test_has_remote_packages_shorthand_string_returns_true() -> None:
    """``github://user/repo@ref`` (and friends) are remote.

    ESPHome accepts a string-shorthand form alongside the dict
    form; ``do_packages_pass`` parses it via
    ``validate_source_shorthand`` which clones from a git remote.
    """
    assert _has_remote_packages({"shared": "github://example/shared@main"}) is True


def test_has_remote_packages_mixed_local_and_remote_returns_true() -> None:
    """Any single remote package in the tree taints the whole config.

    The metadata loader can't merge "just the local ones" — that
    would silently drop blocks the user expects from the remote
    package. Bail to the unmerged shape instead, same as
    pre-fix.
    """
    assert (
        _has_remote_packages(
            {
                "local": {"wifi": {"ssid": "x"}},
                "remote": {"url": "https://github.com/example/shared"},
            }
        )
        is True
    )


def test_has_remote_packages_nested_remote_returns_true() -> None:
    """Remote package nested inside a local one still triggers the gate.

    ESPHome's package resolver flattens nested packages
    recursively — a local package can reference a remote one. The
    walk has to descend the full tree to spot this.
    """
    assert (
        _has_remote_packages(
            {
                "outer": {
                    "packages": {
                        "inner": {
                            "url": "https://github.com/example/inner",
                        }
                    },
                }
            }
        )
        is True
    )


def test_load_device_yaml_falls_back_when_both_imports_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No-op gracefully when neither upstream import shape is available.

    A future esphome that deprecates ``do_packages_pass`` /
    ``merge_packages`` AND moves ``resolve_packages`` (rename,
    refactor, …) would otherwise leave us with no merge path. The
    module's ``try/except ImportError`` guards both imports — the
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


def test_load_device_yaml_skips_merge_for_remote_shorthand_package(
    tmp_path: Path,
) -> None:
    """The git-shorthand string form (``github://``) is also remote.

    ESPHome accepts a ``name: github://user/repo@ref`` value as
    sugar for the dict form. ``_has_remote_packages`` walks string
    entries too, so both shapes hit the gate.
    """
    yaml_file = tmp_path / "remote.yaml"
    yaml_file.write_text(
        "esphome:\n  name: remote\npackages:\n  shared: github://example/shared@main\n"
    )
    with mock.patch("esphome_device_builder.helpers.device_yaml._do_packages_pass") as do_pkg:
        config = load_device_yaml(yaml_file)
    assert config is not None
    assert "packages" in config
    do_pkg.assert_not_called()


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
        "ext_storage_path",
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
