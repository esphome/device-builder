"""Tests for ``helpers.secrets_state.is_wifi_unconfigured``.

Covers every call shape the onboarding controller can hand it:
missing file (``None``), empty dict, missing ``wifi_ssid`` key,
empty-string value, the bootstrap placeholder, a real value, and
a non-string typo.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from esphome import yaml_util
from esphome.core import EsphomeError

from esphome_device_builder.helpers.secrets_state import (
    PLACEHOLDER_WIFI_PASSWORD,
    PLACEHOLDER_WIFI_SSID,
    SecretsContentError,
    _quote_yaml_string,
    is_valid_secret_key,
    is_wifi_unconfigured,
    merge_secrets_file,
    read_secrets_yaml,
    validate_secrets_content,
    wifi_secrets_defined,
    write_secret,
    write_secrets_locked,
)


def test_wifi_secrets_defined_requires_both_keys() -> None:
    """Non-empty ssid + password key ⇒ True; missing either / None / empty ⇒ False."""
    assert wifi_secrets_defined({"wifi_ssid": "x", "wifi_password": "y"}) is True
    # Open network: an empty password is still a defined key.
    assert wifi_secrets_defined({"wifi_ssid": "x", "wifi_password": ""}) is True
    assert wifi_secrets_defined({"wifi_ssid": "x"}) is False
    assert wifi_secrets_defined({"wifi_password": "y"}) is False
    assert wifi_secrets_defined({}) is False
    assert wifi_secrets_defined(None) is False


def test_wifi_secrets_defined_false_for_empty_or_non_string_ssid() -> None:
    """A present-but-empty / blank / non-string ssid would fail cv.ssid, so it's not defined."""
    assert wifi_secrets_defined({"wifi_ssid": "", "wifi_password": "y"}) is False
    assert wifi_secrets_defined({"wifi_ssid": "   ", "wifi_password": "y"}) is False
    assert wifi_secrets_defined({"wifi_ssid": 42, "wifi_password": "y"}) is False


def test_wifi_secrets_defined_false_for_null_or_non_string_password() -> None:
    """A null / non-string password would fail cv.string_strict, so it's not defined."""
    assert wifi_secrets_defined({"wifi_ssid": "x", "wifi_password": None}) is False
    assert wifi_secrets_defined({"wifi_ssid": "x", "wifi_password": 12345678}) is False


def test_wifi_secrets_defined_true_for_placeholder_values() -> None:
    """Seeded placeholders still count as defined — the keys exist, so ``!secret`` resolves."""
    assert (
        wifi_secrets_defined(
            {"wifi_ssid": PLACEHOLDER_WIFI_SSID, "wifi_password": PLACEHOLDER_WIFI_PASSWORD}
        )
        is True
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


def test_validate_secrets_content_rejects_malformed_yaml(tmp_path: Path) -> None:
    """The issue's no-space-after-colon example raises with line info."""
    bad = 'wifi_ssid: "myssid"\nwifi_password: "mypassword"\nxx:xxx\na:a\n'
    with pytest.raises(SecretsContentError) as excinfo:
        validate_secrets_content(bad, tmp_path / "secrets.yaml")
    assert "line 4" in str(excinfo.value)


def test_validate_secrets_content_rejects_duplicate_keys(tmp_path: Path) -> None:
    """ESPHome's loader rejects duplicate keys the plain SafeLoader would accept."""
    with pytest.raises(SecretsContentError, match="Duplicate key"):
        validate_secrets_content("wifi_ssid: a\nwifi_ssid: b\n", tmp_path / "secrets.yaml")


def test_validate_secrets_content_rejects_non_mapping_top_level(tmp_path: Path) -> None:
    """A list or scalar top level isn't the dict ``!secret`` lookups expect."""
    secrets = tmp_path / "secrets.yaml"
    with pytest.raises(SecretsContentError, match="mapping"):
        validate_secrets_content("- a\n- b\n", secrets)
    with pytest.raises(SecretsContentError, match="mapping"):
        validate_secrets_content("just a scalar\n", secrets)


def test_validate_secrets_content_accepts_valid_mapping(tmp_path: Path) -> None:
    validate_secrets_content("wifi_ssid: home\nwifi_password: secret\n", tmp_path / "secrets.yaml")


def test_validate_secrets_content_accepts_include_and_merge_keys(tmp_path: Path) -> None:
    """HA-style secrets with ``!include`` and a merge key resolve, not rejected.

    Includes resolve against the file's own dir, so a present sibling
    passes; this guards the regression a plain SafeLoader would cause by
    rejecting every tagged secrets file.
    """
    (tmp_path / "shared.yaml").write_text("shared_pw: abc\n", encoding="utf-8")
    content = "<<: !include shared.yaml\nwifi_ssid: home\nca_cert: !include ca.pem\n"
    validate_secrets_content(content, tmp_path / "secrets.yaml")


def test_validate_secrets_content_rejects_missing_include_target(tmp_path: Path) -> None:
    """A merge-key include of an absent file fails just like the real read would."""
    secrets = tmp_path / "secrets.yaml"
    with pytest.raises(SecretsContentError):
        validate_secrets_content("<<: !include gone.yaml\nwifi_ssid: home\n", secrets)


def test_validate_secrets_content_wraps_unexpected_loader_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-EsphomeError from the loader (self-referential !secret recurses) rejects, not 500s."""

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(yaml_util, "parse_yaml", _boom)
    with pytest.raises(SecretsContentError, match="could not be parsed"):
        validate_secrets_content("wifi_ssid: home\n", tmp_path / "secrets.yaml")


def test_validate_secrets_content_accepts_comment_only_file(tmp_path: Path) -> None:
    """A comment-only file (empty mapping) is a legitimate secrets.yaml."""
    validate_secrets_content("# nothing here yet\n", tmp_path / "secrets.yaml")


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


# ---------------------------------------------------------------------------
# write_secret — single-key setter
# ---------------------------------------------------------------------------


def _secrets(tmp_path: Path) -> Path:
    return tmp_path / "secrets.yaml"


def test_write_secret_creates_file_and_key(tmp_path: Path) -> None:
    created = write_secret(tmp_path, "api_key", "ABC")
    assert created is True
    assert read_secrets_yaml(tmp_path) == {"api_key": "ABC"}


def test_write_secret_appends_to_existing_file(tmp_path: Path) -> None:
    _secrets(tmp_path).write_text("wifi_ssid: home\n", "utf-8")
    created = write_secret(tmp_path, "api_key", "ABC")
    assert created is True
    assert read_secrets_yaml(tmp_path) == {"wifi_ssid": "home", "api_key": "ABC"}


def test_write_secret_overwrites_existing_key_and_returns_not_created(tmp_path: Path) -> None:
    _secrets(tmp_path).write_text("wifi_ssid: home\napi_key: OLD\n", "utf-8")
    created = write_secret(tmp_path, "api_key", "NEW")
    assert created is False
    assert read_secrets_yaml(tmp_path) == {"wifi_ssid": "home", "api_key": "NEW"}


def test_write_secret_preserves_other_secrets_and_inline_comment(tmp_path: Path) -> None:
    _secrets(tmp_path).write_text("wifi_ssid: home  # Apt 4B\napi_key: OLD\n", "utf-8")
    write_secret(tmp_path, "wifi_ssid", "office")
    assert _secrets(tmp_path).read_text("utf-8") == 'wifi_ssid: "office"  # Apt 4B\napi_key: OLD\n'


def test_write_secret_no_overwrite_leaves_existing_value(tmp_path: Path) -> None:
    _secrets(tmp_path).write_text("api_key: KEEP\n", "utf-8")
    created = write_secret(tmp_path, "api_key", "NEW", overwrite=False)
    assert created is False
    assert read_secrets_yaml(tmp_path) == {"api_key": "KEEP"}


def test_write_secret_no_overwrite_creates_absent_key(tmp_path: Path) -> None:
    _secrets(tmp_path).write_text("api_key: KEEP\n", "utf-8")
    created = write_secret(tmp_path, "mqtt_pw", "secret", overwrite=False)
    assert created is True
    assert read_secrets_yaml(tmp_path) == {"api_key": "KEEP", "mqtt_pw": "secret"}


def test_write_secret_round_trips_a_multiline_value_without_folding(tmp_path: Path) -> None:
    """A value with newlines/tabs/control chars reads back byte-for-byte, not folded."""
    value = "-----BEGIN-----\nline2\twith-tab\x07bell\r\n-----END-----"
    write_secret(tmp_path, "cert", value)
    assert read_secrets_yaml(tmp_path) == {"cert": value}


def test_quote_yaml_string_escapes_named_and_hex_control_chars() -> None:
    assert _quote_yaml_string("a\nb\tc\x07d") == '"a\\nb\\tc\\x07d"'


async def test_write_secrets_locked_runs_under_the_lock_and_returns() -> None:
    """The funnel holds the lock (blocking a held one) and returns the fn's result."""
    lock = asyncio.Lock()
    await lock.acquire()
    task = asyncio.create_task(write_secrets_locked(lock, lambda: "done"))
    await asyncio.sleep(0)
    assert not task.done()  # blocked on the held lock
    lock.release()
    assert await task == "done"


@pytest.mark.parametrize("key", ["wifi_ssid", "_x", "ABC123", "a"])
def test_is_valid_secret_key_accepts_identifier_keys(key: str) -> None:
    assert is_valid_secret_key(key) is True


@pytest.mark.parametrize("key", ["", "1abc", "with-dash", "has space", "a:b", "x\n", "no#hash"])
def test_is_valid_secret_key_rejects_non_identifier_keys(key: str) -> None:
    assert is_valid_secret_key(key) is False
