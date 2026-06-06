"""Tests for ``helpers.secrets_state.is_wifi_unconfigured``.

Covers every call shape the onboarding controller can hand it:
missing file (``None``), empty dict, missing ``wifi_ssid`` key,
empty-string value, the bootstrap placeholder, a real value, and
a non-string typo.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from esphome.core import EsphomeError

from esphome_device_builder.helpers.secrets_state import (
    PLACEHOLDER_WIFI_PASSWORD,
    PLACEHOLDER_WIFI_SSID,
    is_wifi_unconfigured,
    merge_secrets_file,
    read_secrets_yaml,
)


def test_unconfigured_when_secrets_is_none() -> None:
    """File missing entirely ⇒ user needs to set credentials."""
    assert is_wifi_unconfigured(None) is True


def test_unconfigured_when_secrets_is_empty_dict() -> None:
    """File present but empty ⇒ same as missing for our purposes."""
    assert is_wifi_unconfigured({}) is True


def test_unconfigured_when_wifi_ssid_key_is_missing() -> None:
    """Other secrets present but no ``wifi_ssid`` ⇒ unconfigured."""
    assert is_wifi_unconfigured({"api_key": "ZZZ", "mqtt_pw": "shhh"}) is True


def test_unconfigured_when_wifi_ssid_is_empty_string() -> None:
    """Existing installs from the previous bootstrap ⇒ still unconfigured."""
    assert is_wifi_unconfigured({"wifi_ssid": ""}) is True


def test_unconfigured_when_wifi_ssid_is_only_whitespace() -> None:
    """``"  "`` should be treated like empty — strip before comparing."""
    assert is_wifi_unconfigured({"wifi_ssid": "   "}) is True


def test_unconfigured_when_wifi_ssid_matches_bootstrap_placeholder() -> None:
    """Fresh-install placeholder ⇒ user hasn't replaced it yet."""
    assert is_wifi_unconfigured({"wifi_ssid": PLACEHOLDER_WIFI_SSID}) is True


def test_configured_when_wifi_ssid_is_a_real_value() -> None:
    assert is_wifi_unconfigured({"wifi_ssid": "home_network"}) is False


def test_unconfigured_when_wifi_ssid_is_a_non_string_typo() -> None:
    """``wifi_ssid: 42`` (missing quotes) — keep onboarding visible.

    ESPHome's compile-time validator rejects non-string SSID, so
    treating this as 'configured' would clear the onboarding
    badge while the actual config is still broken — the user
    would never see the prompt that would have helped them.
    """
    assert is_wifi_unconfigured({"wifi_ssid": 42}) is True
    assert is_wifi_unconfigured({"wifi_ssid": ["x"]}) is True
    assert is_wifi_unconfigured({"wifi_ssid": None}) is True


@pytest.mark.parametrize(
    "value",
    ["MyNetwork", "REPLACE_WITH_OTHER_THING", "  spaced  network  "],
)
def test_password_does_not_affect_configured_state(value: str) -> None:
    """Password value is intentionally not part of the check.

    Open networks legitimately have an empty password.
    """
    assert is_wifi_unconfigured({"wifi_ssid": value, "wifi_password": ""}) is False


def test_read_secrets_yaml_returns_none_for_missing_file(tmp_path: Path) -> None:
    """Fail-soft contract: missing file ⇒ None, not raise."""
    assert read_secrets_yaml(tmp_path) is None


def test_read_secrets_yaml_returns_none_for_malformed_file(
    tmp_path: Path,
) -> None:
    """Parse error ⇒ None.

    Both readers (config + onboarding) fall back to safe empty /
    unconfigured states.
    """
    (tmp_path / "secrets.yaml").write_text("wifi_ssid: [unclosed\n")
    assert read_secrets_yaml(tmp_path) is None


def test_read_secrets_yaml_returns_none_for_non_dict_top_level(
    tmp_path: Path,
) -> None:
    """Reject non-dict top-level YAML.

    A list or scalar at the top level isn't a usable secrets
    file — treat as None so callers fall back.
    """
    (tmp_path / "secrets.yaml").write_text("- not\n- a\n- mapping\n")
    assert read_secrets_yaml(tmp_path) is None


def test_read_secrets_yaml_returns_dict_for_valid_file(tmp_path: Path) -> None:
    (tmp_path / "secrets.yaml").write_text("wifi_ssid: home\nwifi_password: secret\napi_key: ABC\n")
    data = read_secrets_yaml(tmp_path)
    assert data is not None
    assert data["wifi_ssid"] == "home"
    assert data["api_key"] == "ABC"


def test_placeholder_password_constant_is_exported() -> None:
    """Pin the placeholder password export.

    The constant is unused by ``is_wifi_unconfigured`` but
    exported alongside the SSID one because the bootstrap and
    the onboarding setter both need it. Locking the export here
    prevents a future refactor from silently moving it.
    """
    assert isinstance(PLACEHOLDER_WIFI_PASSWORD, str)
    assert PLACEHOLDER_WIFI_PASSWORD


def test_merge_secrets_creates_when_dest_absent(tmp_path: Path) -> None:
    """A missing dest is created from the source bytes verbatim."""
    src = tmp_path / "src.yaml"
    src.write_text("wifi_password: p\n", "utf-8")
    dest = tmp_path / "secrets.yaml"

    merge_secrets_file(src, dest)

    assert dest.read_text("utf-8") == "wifi_password: p\n"


def test_merge_secrets_appends_only_absent_keys(tmp_path: Path) -> None:
    """Existing keys/comments are preserved; only absent keys are appended."""
    src = tmp_path / "src.yaml"
    src.write_text("wifi_password: new\napi_key: k\n", "utf-8")
    dest = tmp_path / "secrets.yaml"
    dest.write_text("# secrets\nwifi_password: original  # note\n", "utf-8")

    merge_secrets_file(src, dest)

    merged = dest.read_text("utf-8")
    assert "# secrets" in merged
    assert "wifi_password: original  # note" in merged
    assert "api_key: k" in merged
    assert "new" not in merged


def test_merge_secrets_noop_when_no_absent_keys(tmp_path: Path) -> None:
    """When the bundle adds no new keys, the dest is left byte-for-byte."""
    src = tmp_path / "src.yaml"
    src.write_text("wifi_password: x\n", "utf-8")
    dest = tmp_path / "secrets.yaml"
    original = "wifi_password: original\n"
    dest.write_text(original, "utf-8")

    merge_secrets_file(src, dest)

    assert dest.read_text("utf-8") == original


def test_merge_secrets_leaves_non_mapping_untouched(tmp_path: Path) -> None:
    """A list/scalar secrets file is never overwritten by the merge."""
    src = tmp_path / "src.yaml"
    src.write_text("wifi_password: x\n", "utf-8")
    dest = tmp_path / "secrets.yaml"
    original = "- not\n- a\n- mapping\n"
    dest.write_text(original, "utf-8")

    merge_secrets_file(src, dest)

    assert dest.read_text("utf-8") == original


def test_merge_secrets_leaves_unreadable_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A secrets file the tolerant loader can't read is left untouched."""
    src = tmp_path / "src.yaml"
    src.write_text("wifi_password: x\n", "utf-8")
    dest = tmp_path / "secrets.yaml"
    original = "wifi_password: original\n"
    dest.write_text(original, "utf-8")

    def _boom(_path: Path) -> object:
        raise EsphomeError("can't read this")

    monkeypatch.setattr(
        "esphome_device_builder.helpers.secrets_state.load_yaml_fast_then_esphome",
        _boom,
    )

    merge_secrets_file(src, dest)

    assert dest.read_text("utf-8") == original
