"""Tests for ``helpers.secrets_state``.

Covers the Wi-Fi state predicate (``wifi_secrets_defined``), the shared
``validate_wifi_credentials`` guard, the placeholder migration, the read /
merge / line-based write helpers, and the secret-key rules.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from esphome import yaml_util
from esphome.core import EsphomeError

from esphome_device_builder.helpers.secrets_state import (
    MAX_SSID_LEN,
    MAX_WIFI_PASSWORD_LEN,
    PLACEHOLDER_WIFI_PASSWORD,
    PLACEHOLDER_WIFI_SSID,
    SecretsContentError,
    _quote_yaml_string,
    _replace_or_append_secret,
    is_valid_secret_key,
    merge_secrets_file,
    migrate_placeholder_wifi_secrets,
    read_secrets_yaml,
    validate_secrets_content,
    validate_wifi_credentials,
    wifi_secrets_defined,
    write_secret,
    write_secrets_locked,
    write_wifi_secrets,
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

    Kept live by ``migrate_placeholder_wifi_secrets`` (it strips a leftover
    seeded value only when it equals this exact placeholder). Locking the
    export here prevents a future refactor from silently moving it.
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


# ---------------------------------------------------------------------------
# validate_wifi_credentials — shared by config/set_wifi_credentials + create
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ssid", "password"),
    [
        ("MyAP", "hunter2"),
        ("OpenNet", ""),  # open network: empty password allowed
        ("  spaced  ", "p"),  # 802.11 allows leading/trailing whitespace
        ("MyAP", "hunter\t2"),  # TAB is the one allowed control char
        ("A" * MAX_SSID_LEN, "P" * MAX_WIFI_PASSWORD_LEN),  # at the caps
    ],
)
def test_validate_wifi_credentials_accepts_valid(ssid: str, password: str) -> None:
    validate_wifi_credentials(ssid, password)


@pytest.mark.parametrize(
    ("ssid", "password", "match"),
    [
        ("   ", "p", "SSID can't be empty"),
        (42, "p", "SSID must be a string"),
        ("MyAP", None, "Password must be a string"),
        ("A" * (MAX_SSID_LEN + 1), "p", "32 characters"),
        ("MyAP", "P" * (MAX_WIFI_PASSWORD_LEN + 1), "64 characters"),
        ("My\nNet", "p", "control character"),
        ("My\x00Net", "p", "control character"),
        ("MyAP", "p\rass", "control character"),
    ],
)
def test_validate_wifi_credentials_rejects_invalid(
    ssid: object, password: object, match: str
) -> None:
    with pytest.raises(SecretsContentError, match=match):
        validate_wifi_credentials(ssid, password)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# migrate_placeholder_wifi_secrets — one-shot cleanup for existing installs
# ---------------------------------------------------------------------------


def test_migrate_placeholder_wifi_removes_seeded_placeholders(tmp_path: Path) -> None:
    _secrets(tmp_path).write_text(
        f'# secrets\napi_key: ABC\nwifi_ssid: "{PLACEHOLDER_WIFI_SSID}"\n'
        f'wifi_password: "{PLACEHOLDER_WIFI_PASSWORD}"\n',
        "utf-8",
    )
    migrate_placeholder_wifi_secrets(tmp_path)
    assert read_secrets_yaml(tmp_path) == {"api_key": "ABC"}
    # Unrelated content is preserved.
    assert "# secrets" in _secrets(tmp_path).read_text("utf-8")


def test_migrate_placeholder_wifi_keeps_real_values(tmp_path: Path) -> None:
    _secrets(tmp_path).write_text("wifi_ssid: home\nwifi_password: hunter2\n", "utf-8")
    migrate_placeholder_wifi_secrets(tmp_path)
    assert read_secrets_yaml(tmp_path) == {"wifi_ssid": "home", "wifi_password": "hunter2"}


def test_migrate_placeholder_wifi_drops_only_placeholder_key(tmp_path: Path) -> None:
    """A real SSID with a placeholder password drops only the placeholder."""
    _secrets(tmp_path).write_text(
        f'wifi_ssid: home\nwifi_password: "{PLACEHOLDER_WIFI_PASSWORD}"\n', "utf-8"
    )
    migrate_placeholder_wifi_secrets(tmp_path)
    assert read_secrets_yaml(tmp_path) == {"wifi_ssid": "home"}


def test_migrate_placeholder_wifi_noop_on_missing_file(tmp_path: Path) -> None:
    migrate_placeholder_wifi_secrets(tmp_path)
    assert not _secrets(tmp_path).exists()


def test_migrate_placeholder_wifi_noop_on_comment_only_file(tmp_path: Path) -> None:
    """A comment-only / empty secrets.yaml parses to nothing — leave it untouched."""
    original = "# nothing here yet\n"
    _secrets(tmp_path).write_text(original, "utf-8")
    migrate_placeholder_wifi_secrets(tmp_path)
    assert _secrets(tmp_path).read_text("utf-8") == original


# ---------------------------------------------------------------------------
# write_wifi_secrets — line-based two-key setter
# ---------------------------------------------------------------------------


def test_write_wifi_secrets_creates_file_when_missing(tmp_path: Path) -> None:
    write_wifi_secrets(tmp_path, "MyAP", "secret")
    assert read_secrets_yaml(tmp_path) == {"wifi_ssid": "MyAP", "wifi_password": "secret"}


def test_write_wifi_secrets_preserves_other_keys_and_comments(tmp_path: Path) -> None:
    _secrets(tmp_path).write_text("# top\napi_key: ABC\nwifi_ssid: old  # note\n", "utf-8")
    write_wifi_secrets(tmp_path, "new_ap", "pw")
    content = _secrets(tmp_path).read_text("utf-8")
    assert "# top" in content
    assert "api_key: ABC" in content
    assert 'wifi_ssid: "new_ap"  # note' in content
    assert 'wifi_password: "pw"' in content


def test_write_wifi_secrets_escapes_double_quotes(tmp_path: Path) -> None:
    write_wifi_secrets(tmp_path, 'Net"x', "p")
    assert r'wifi_ssid: "Net\"x"' in _secrets(tmp_path).read_text("utf-8")


# ---------------------------------------------------------------------------
# _replace_or_append_secret — direct unit tests
# ---------------------------------------------------------------------------
#
# Isolated coverage of the fiddly line regex the writers lean on. Anyone
# refactoring ``_SECRET_LINE_RE`` should see these break first.


def test_replace_or_append_secret_appends_when_key_absent_in_existing_file() -> None:
    """File exists with other keys — new key gets appended, not inlined."""
    result = _replace_or_append_secret("api_key: ABC\n", "wifi_ssid", "MyAP")
    assert result == 'api_key: ABC\nwifi_ssid: "MyAP"\n'


def test_replace_or_append_secret_appends_to_file_without_trailing_newline() -> None:
    """No trailing newline on input — helper adds one before appending."""
    result = _replace_or_append_secret("api_key: ABC", "wifi_ssid", "MyAP")
    assert result == 'api_key: ABC\nwifi_ssid: "MyAP"\n'


def test_replace_or_append_secret_appends_to_empty_content() -> None:
    """Empty input behaves like the missing-file path."""
    assert _replace_or_append_secret("", "wifi_ssid", "MyAP") == 'wifi_ssid: "MyAP"\n'


def test_replace_or_append_secret_preserves_indent() -> None:
    """Indented secret lines keep their indent on rewrite."""
    result = _replace_or_append_secret('  wifi_ssid: "old"\n', "wifi_ssid", "new")
    assert result == '  wifi_ssid: "new"\n'


def test_replace_or_append_secret_quotes_special_characters() -> None:
    """Backslash and double-quote in the value get escaped, others pass through."""
    result = _replace_or_append_secret('wifi_password: "old"\n', "wifi_password", 'p\\a"s s')
    assert result == 'wifi_password: "p\\\\a\\"s s"\n'


def test_replace_or_append_secret_only_matches_full_key_name() -> None:
    r"""``wifi_ssid_backup`` is not the same key as ``wifi_ssid``."""
    result = _replace_or_append_secret('wifi_ssid_backup: "keep"\n', "wifi_ssid", "MyAP")
    assert 'wifi_ssid_backup: "keep"' in result
    assert 'wifi_ssid: "MyAP"' in result


def test_replace_or_append_secret_ignores_pure_comment_lines() -> None:
    """A standalone ``# wifi_ssid: foo`` comment is not a key."""
    result = _replace_or_append_secret(
        '# wifi_ssid: "example"\napi_key: ABC\n', "wifi_ssid", "MyAP"
    )
    assert '# wifi_ssid: "example"' in result
    assert 'wifi_ssid: "MyAP"' in result


def test_replace_or_append_secret_preserves_inline_comment_with_special_chars() -> None:
    """Trailing ``# comment with : colons`` round-trips intact."""
    result = _replace_or_append_secret(
        'wifi_ssid: "old"  # see ticket: ABC-123\n', "wifi_ssid", "MyAP"
    )
    assert result == 'wifi_ssid: "MyAP"  # see ticket: ABC-123\n'


def test_replace_or_append_secret_handles_bare_key() -> None:
    """``wifi_ssid:`` with no value still matches and gets the new value."""
    result = _replace_or_append_secret("wifi_ssid:\n", "wifi_ssid", "MyAP")
    assert result == 'wifi_ssid: "MyAP"\n'


def test_replace_or_append_secret_value_with_hash_in_quotes_is_misparsed() -> None:
    """Known limitation: ``# `` inside a quoted value confuses the regex.

    The result is still valid YAML; the spurious tail is preserved as a
    comment. Pin the behaviour so a future regex tightening that fixes it
    has a green-then-red breadcrumb.
    """
    result = _replace_or_append_secret('wifi_ssid: "foo # bar"\n', "wifi_ssid", "MyAP")
    assert result == 'wifi_ssid: "MyAP" # bar"\n'
