"""Tests for the esphome OTA platform ``encryption.key`` reader and its api-key follow-along."""

from __future__ import annotations

import pytest

from esphome_device_builder.helpers.yaml import (
    YamlUpsertNotSupportedError,
    read_ota_encryption_key,
    rewrite_api_encryption_key,
    upsert_api_encryption_key,
)
from esphome_device_builder.helpers.yaml.ota_encryption import drop_ota_encryption_key

NEW = "bmV3a2V5bmV3a2V5bmV3a2V5bmV3a2V5bmV3a2V5bmV3a2V5bmU="
API = 'api:\n  encryption:\n    key: "oldkey"\n\n'

LIST_FORM = API + (
    'ota:\n  - platform: esphome\n    encryption:\n      key: "oldkey"  # keep me\n    port: 3232\n'
)
MAPPING_FORM = API + "ota:\n  platform: esphome\n  encryption:\n    key: oldkey\n"
BARE_MAPPING_FORM = API + "ota:\n  encryption:\n    key: oldkey\n"
SECOND_ITEM = (
    API + "ota:\n  - platform: web_server\n  - platform: esphome\n    password: hunter2\n"
    "    encryption:\n      key: 'oldkey'\n"
)
BARE_ENCRYPTION = API + "ota:\n  - platform: esphome\n    encryption:\n"
OTHER_PLATFORM_ONLY = API + "ota:\n  - platform: web_server\n    encryption:\n      key: oldkey\n"


@pytest.mark.parametrize(
    ("yaml_text", "expected"),
    [
        pytest.param(LIST_FORM, '"oldkey"', id="list"),
        pytest.param(MAPPING_FORM, "oldkey", id="mapping"),
        pytest.param(BARE_MAPPING_FORM, None, id="mapping-no-platform"),
        pytest.param(SECOND_ITEM, "'oldkey'", id="second-item"),
        pytest.param(
            "ota:\n  -\n    platform: esphome\n    encryption:\n      key: oldkey\n",
            "oldkey",
            id="bare-dash-item",
        ),
        pytest.param(
            "ota:\n  - id: x\n    encryption:\n      key: oldkey\n    platform: esphome\n",
            "oldkey",
            id="platform-after-nested-block",
        ),
        pytest.param("ota:\n  -\n    # nothing yet\n", None, id="bare-dash-empty"),
        pytest.param(
            "ota:\n  platform: esphome\n  on_error:\n    - logger.log: fail\n"
            "  encryption:\n    key: oldkey\n",
            "oldkey",
            id="mapping-with-action-list",
        ),
        pytest.param(
            "ota:\n  platform: web_server\n  encryption:\n    key: oldkey\n",
            None,
            id="mapping-other-platform",
        ),
        pytest.param("ota: !include common/ota.yaml\n", None, id="include-header"),
        pytest.param("ota: !remove\n", None, id="remove-header"),
        pytest.param("ota: {platform: esphome}\n", None, id="flow-header"),
        pytest.param(BARE_ENCRYPTION, None, id="bare-encryption"),
        pytest.param(OTHER_PLATFORM_ONLY, None, id="other-platform"),
        pytest.param("ota:\n  - platform: esphome\n    password: x\n", None, id="no-encryption"),
        pytest.param(API, None, id="no-ota"),
        pytest.param("", None, id="empty"),
    ],
)
def test_read_ota_encryption_key(yaml_text: str, expected: str | None) -> None:
    assert read_ota_encryption_key(yaml_text) == expected


def test_ota_key_collapses_to_a_bare_block_on_api_rewrite() -> None:
    out = rewrite_api_encryption_key(LIST_FORM, NEW)
    assert out.count(f'key: "{NEW}"') == 1
    assert "  - platform: esphome\n    encryption:\n    port: 3232\n" in out
    assert "oldkey" not in out


def test_ota_key_collapses_in_mapping_form() -> None:
    out = upsert_api_encryption_key(MAPPING_FORM, NEW)
    assert out.endswith("ota:\n  platform: esphome\n  encryption:\n")


def test_ota_key_collapses_skipping_other_platform() -> None:
    out = upsert_api_encryption_key(SECOND_ITEM, NEW)
    assert "  - platform: web_server\n" in out
    assert out.count(f'key: "{NEW}"') == 1
    assert out.endswith("    password: hunter2\n    encryption:\n")
    assert "oldkey" not in out


def test_stale_ota_key_is_dropped_when_api_already_matches() -> None:
    yaml_text = LIST_FORM.replace('key: "oldkey"\n', f'key: "{NEW}"\n', 1)
    out = upsert_api_encryption_key(yaml_text, NEW)
    assert out.count(f'key: "{NEW}"') == 1
    assert "oldkey" not in out


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("!secret ota_key", id="secret"),
        pytest.param('"!secret ota_key"', id="quoted-secret"),
        pytest.param("${ota_key}", id="substitution"),
        pytest.param('"${ota_key}"', id="quoted-substitution"),
    ],
)
def test_indirected_ota_key_refuses_the_api_rewrite(value: str) -> None:
    yaml_text = API + f"ota:\n  - platform: esphome\n    encryption:\n      key: {value}\n"
    with pytest.raises(YamlUpsertNotSupportedError, match="OTA encryption key"):
        upsert_api_encryption_key(yaml_text, NEW)


def test_inserting_api_key_next_to_a_differing_own_ota_key_is_refused() -> None:
    yaml_text = (
        "api:\n  encryption:\n\nota:\n  - platform: esphome\n    encryption:\n      key: ownkey\n"
    )
    with pytest.raises(YamlUpsertNotSupportedError, match="own encryption key"):
        upsert_api_encryption_key(yaml_text, NEW)


def test_inserting_api_key_next_to_an_indirected_own_ota_key_names_the_indirection() -> None:
    ota = "ota:\n  - platform: esphome\n    encryption:\n      key: !secret ota\n"
    yaml_text = "api:\n  encryption:\n\n" + ota
    with pytest.raises(YamlUpsertNotSupportedError, match="cannot be checked"):
        upsert_api_encryption_key(yaml_text, NEW)


def test_inserting_api_key_next_to_a_matching_own_ota_key_collapses_it() -> None:
    yaml_text = (
        f"api:\n  encryption:\n\nota:\n  - platform: esphome\n    encryption:\n      key: {NEW}\n"
    )
    out = upsert_api_encryption_key(yaml_text, NEW)
    assert out.count(NEW) == 1
    assert out.startswith(f'api:\n  encryption:\n    key: "{NEW}"\n')
    assert out.endswith("    encryption:\n")


def test_unreadable_ota_header_leaves_the_api_rewrite_alone() -> None:
    yaml_text = API + "ota: !include common/ota.yaml\n"
    out = rewrite_api_encryption_key(yaml_text, NEW)
    assert out == yaml_text.replace('key: "oldkey"', f'key: "{NEW}"')


def test_indirected_api_key_leaves_ota_key_alone() -> None:
    yaml_text = LIST_FORM.replace('key: "oldkey"\n', "key: !secret api_key\n", 1)
    assert rewrite_api_encryption_key(yaml_text, NEW) == yaml_text


@pytest.mark.parametrize(
    "yaml_text",
    [
        pytest.param(BARE_ENCRYPTION, id="bare-encryption"),
        pytest.param(OTHER_PLATFORM_ONLY, id="other-platform"),
        pytest.param(API + "esphome:\n  name: x\n", id="no-ota"),
    ],
)
def test_api_rewrite_without_explicit_ota_key_touches_api_only(yaml_text: str) -> None:
    out = rewrite_api_encryption_key(yaml_text, NEW)
    assert out.count(NEW) == 1
    assert out.replace(f'key: "{NEW}"', 'key: "oldkey"', 1) == yaml_text


def test_ota_drop_preserves_crlf() -> None:
    yaml_text = API.replace("\n", "\r\n") + (
        "ota:\r\n  - platform: esphome\r\n    encryption:\r\n      key: oldkey\r\n"
    )
    out = rewrite_api_encryption_key(yaml_text, NEW)
    assert out.endswith("  - platform: esphome\r\n    encryption:\r\n")
    assert "\n" not in out.replace("\r\n", "")


def test_drop_without_an_encryption_block_is_a_noop() -> None:
    yaml_text = "ota:\n  - platform: esphome\n"
    assert drop_ota_encryption_key(yaml_text) == yaml_text


@pytest.mark.parametrize("empty", ["", '""'], ids=["bare", "quoted"])
def test_empty_keys_are_filled_in(empty: str) -> None:
    api = f"api:\n  encryption:\n    key: {empty}\n"
    ota = f"ota:\n  - platform: esphome\n    encryption:\n      key: {empty}\n"
    out = upsert_api_encryption_key((api + ota).replace("key: \n", "key:\n"), NEW)
    assert out.count(f'key: "{NEW}"') == 1
    assert out.endswith("    encryption:\n")


def test_inserting_api_key_drops_an_empty_own_ota_key() -> None:
    yaml_text = "api:\n  encryption:\n\nota:\n  - platform: esphome\n    encryption:\n      key:\n"
    out = upsert_api_encryption_key(yaml_text, NEW)
    assert out.count(f'key: "{NEW}"') == 1
    assert out.endswith("    encryption:\n")


@pytest.mark.parametrize(
    "ota_key_lines",
    [
        pytest.param("      key: >-\n        oldkey\n", id="block-scalar"),
        pytest.param("      key:\n        oldkey\n", id="value-on-next-line"),
    ],
)
def test_multi_line_ota_key_refuses_the_drop(ota_key_lines: str) -> None:
    yaml_text = API + "ota:\n  - platform: esphome\n    encryption:\n" + ota_key_lines
    with pytest.raises(YamlUpsertNotSupportedError, match="more than one line"):
        rewrite_api_encryption_key(yaml_text, NEW)


def test_drop_keeps_a_comment_line_between_key_and_sibling() -> None:
    ota = "ota:\n  - platform: esphome\n    encryption:\n"
    yaml_text = API + ota + "      key: oldkey\n      # note\n    port: 1\n"
    out = rewrite_api_encryption_key(yaml_text, NEW)
    assert out.endswith("    encryption:\n      # note\n    port: 1\n")
