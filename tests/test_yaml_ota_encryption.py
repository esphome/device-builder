"""Tests for the esphome OTA platform ``encryption.key`` reader and its api-key follow-along."""

from __future__ import annotations

import pytest

from esphome_device_builder.helpers.device_yaml import yaml_declares_ota_old_key
from esphome_device_builder.helpers.yaml import (
    OldKeyOutcome,
    YamlUpsertNotSupportedError,
    api_key_settled,
    drop_ota_encryption_old_key,
    read_ota_encryption_key,
    read_ota_encryption_old_key,
    rewrite_api_encryption_key,
    rewrite_own_ota_encryption_key,
    rotate_api_encryption_key,
    upsert_api_encryption_key,
)
from esphome_device_builder.helpers.yaml.api_encryption import _ota_key_competes
from esphome_device_builder.helpers.yaml.ota_encryption import (
    drop_ota_encryption_key,
    set_ota_encryption_old_key,
)

NEW = "bmV3a2V5bmV3a2V5bmV3a2V5bmV3a2V5bmV3a2V5bmV3a2V5bmU="
API = 'api:\n  encryption:\n    key: "oldkey"\n\n'

LIST_FORM = API + (
    'ota:\n  - platform: esphome\n    encryption:\n      key: "oldkey"  # dropped\n    port: 3232\n'
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
        # Known limitation: the top-block walker ends a block at a column-0 dash.
        pytest.param(
            'ota:\n- platform: esphome\n  encryption:\n    key: "oldkey"\n',
            None,
            id="flush-dash-list",
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
    with pytest.raises(YamlUpsertNotSupportedError, match="must match the api"):
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


def test_own_ota_key_is_rekeyed_without_a_literal_api_key() -> None:
    yaml_text = (
        "mqtt:\n  broker: b\n\nota:\n  - platform: esphome\n    encryption:\n      key: oldkey\n"
    )
    out = rewrite_own_ota_encryption_key(yaml_text, NEW)
    assert out.endswith(f'    encryption:\n      key: "{NEW}"\n')
    assert "oldkey" not in out


@pytest.mark.parametrize(
    "yaml_text",
    [
        pytest.param(LIST_FORM, id="literal-api-key"),
        pytest.param(
            LIST_FORM.replace('key: "oldkey"\n', "key: !secret api_key\n", 1),
            id="indirected-api-key",
        ),
        pytest.param(
            LIST_FORM.replace('key: "oldkey"\n', "key: ${api_key}\n", 1),
            id="substituted-api-key",
        ),
        pytest.param(
            "ota:\n  - platform: esphome\n    encryption:\n      key: !secret k\n", id="secret"
        ),
        pytest.param(BARE_ENCRYPTION, id="bare"),
        pytest.param("mqtt:\n  broker: b\n", id="no-ota"),
    ],
)
def test_own_ota_key_rekey_leaves_other_shapes_alone(yaml_text: str) -> None:
    assert rewrite_own_ota_encryption_key(yaml_text, NEW) == yaml_text


def test_empty_api_key_next_to_a_differing_own_ota_key_is_refused() -> None:
    ota = "ota:\n  - platform: esphome\n    encryption:\n      key: ownkey\n"
    yaml_text = 'api:\n  encryption:\n    key: ""\n\n' + ota
    with pytest.raises(YamlUpsertNotSupportedError, match="own encryption key"):
        upsert_api_encryption_key(yaml_text, NEW)


@pytest.mark.parametrize(
    ("yaml_text", "expected"),
    [
        pytest.param(API, False, id="no_ota_key"),
        pytest.param(LIST_FORM.replace('"oldkey"  # dropped', '"k"'), False, id="literal_matches"),
        pytest.param(LIST_FORM, True, id="literal_differs"),
        pytest.param(LIST_FORM.replace('"oldkey"  # dropped', "!secret k"), False, id="indirected"),
        pytest.param(LIST_FORM.replace('"oldkey"  # dropped', ""), False, id="empty"),
        pytest.param(LIST_FORM.replace('"oldkey"  # dropped', '""'), False, id="quoted_empty"),
    ],
)
def test_ota_key_competes(yaml_text: str, expected: bool) -> None:
    assert _ota_key_competes(yaml_text, "k") is expected


BARE_OTA = BARE_ENCRYPTION.removeprefix(API)
OLD_KEY_LINE = '      old_key: "oldkey"\n'


def _rotate(
    yaml_text: str, new_key: str, *, record_previous: bool, replace_old_key: bool = False
) -> str:
    return rotate_api_encryption_key(
        yaml_text, new_key, record_previous=record_previous, replace_old_key=replace_old_key
    )[0]


@pytest.mark.parametrize(
    ("yaml_text", "expected_tail"),
    [
        pytest.param(BARE_ENCRYPTION, "    encryption:\n" + OLD_KEY_LINE, id="bare-list"),
        pytest.param(
            LIST_FORM, "    encryption:\n" + OLD_KEY_LINE + "    port: 3232\n", id="keyed-list"
        ),
        pytest.param(MAPPING_FORM, '  encryption:\n    old_key: "oldkey"\n', id="mapping"),
        pytest.param(
            SECOND_ITEM, "    password: hunter2\n    encryption:\n" + OLD_KEY_LINE, id="second-item"
        ),
        pytest.param(
            BARE_ENCRYPTION + "\n    port: 3232\n",
            "    encryption:\n" + OLD_KEY_LINE + "\n    port: 3232\n",
            id="blank-before-sibling",
        ),
        pytest.param(
            BARE_ENCRYPTION + "      # note\n    port: 1\n",
            "    encryption:\n      # note\n" + OLD_KEY_LINE + "    port: 1\n",
            id="deeper-comment-child",
        ),
        pytest.param(
            API + "ota:\n    - platform: esphome\n      encryption:\n          key: oldkey\n",
            '      encryption:\n          old_key: "oldkey"\n',
            id="four-space-indent",
        ),
        pytest.param(
            BARE_ENCRYPTION.rstrip("\n"),
            "    encryption:\n" + OLD_KEY_LINE,
            id="no-trailing-newline",
        ),
    ],
)
def test_previous_key_is_recorded_as_old_key(yaml_text: str, expected_tail: str) -> None:
    out = _rotate(yaml_text, NEW, record_previous=True)
    assert out.count(f'key: "{NEW}"') == 1
    assert out.endswith(expected_tail)
    assert "# dropped" not in out


def test_old_key_preserves_crlf() -> None:
    yaml_text = API.replace("\n", "\r\n") + (
        "ota:\r\n  - platform: esphome\r\n    encryption:\r\n      key: oldkey\r\n"
    )
    out = _rotate(yaml_text, NEW, record_previous=True)
    assert out.endswith('    encryption:\r\n      old_key: "oldkey"\r\n')
    assert "\n" not in out.replace("\r\n", "")


@pytest.mark.parametrize(
    ("old_key_line", "replace", "expected_tail"),
    [
        pytest.param(
            '      old_key: "older"\n',
            False,
            '    encryption:\n      old_key: "older"\n',
            id="retained",
        ),
        pytest.param(
            '      old_key: "older"\n', True, "    encryption:\n" + OLD_KEY_LINE, id="replaced"
        ),
        pytest.param(
            "      old_key: oldkey  # kept verbatim\n",
            True,
            "    encryption:\n      old_key: oldkey  # kept verbatim\n",
            id="equal-to-previous-left-as-is",
        ),
        pytest.param(
            "      old_key: !secret previous_key\n",
            True,
            "    encryption:\n      old_key: !secret previous_key\n",
            id="indirected-never-overwritten",
        ),
    ],
)
def test_existing_old_key(old_key_line: str, replace: bool, expected_tail: str) -> None:
    out = _rotate(
        BARE_ENCRYPTION + old_key_line, NEW, record_previous=True, replace_old_key=replace
    )
    assert out.count(f'key: "{NEW}"') == 1
    assert out.endswith(expected_tail)


@pytest.mark.parametrize("keep", [True, False], ids=["keep", "default"])
def test_old_key_equal_to_new_key_is_dropped(keep: bool) -> None:
    yaml_text = (
        API.replace("oldkey", NEW) + BARE_OTA + f'      key: "{NEW}"\n      old_key: "{NEW}"\n'
    )
    out = _rotate(yaml_text, NEW, record_previous=keep)
    assert out.count(NEW) == 1
    assert out.endswith("    encryption:\n")


def test_differing_old_key_is_kept_when_api_already_matches() -> None:
    yaml_text = (
        API.replace("oldkey", NEW) + BARE_OTA + '      key: "stale"\n      old_key: "older"\n'
    )
    out = _rotate(yaml_text, NEW, record_previous=True)
    assert out.endswith('    encryption:\n      old_key: "older"\n')


@pytest.mark.parametrize("empty", ["", '""'], ids=["bare", "quoted"])
def test_old_key_not_written_for_empty_previous_key(empty: str) -> None:
    api = f"api:\n  encryption:\n    key: {empty}\n".replace("key: \n", "key:\n")
    out = _rotate(api + BARE_OTA, NEW, record_previous=True)
    assert "old_key" not in out
    assert out.endswith("    encryption:\n")


@pytest.mark.parametrize(
    "yaml_text",
    [
        pytest.param(API + "ota:\n  - platform: esphome\n    password: x\n", id="no-encryption"),
        pytest.param(OTHER_PLATFORM_ONLY, id="other-platform"),
        pytest.param(API + "ota: !include common/ota.yaml\n", id="include-header"),
        pytest.param(API + "esphome:\n  name: x\n", id="no-ota"),
    ],
)
def test_old_key_not_written_without_an_encryption_block(yaml_text: str) -> None:
    out = _rotate(yaml_text, NEW, record_previous=True)
    assert out == rewrite_api_encryption_key(yaml_text, NEW)
    assert "old_key" not in out


@pytest.mark.parametrize(
    "rewrite", [rewrite_api_encryption_key, upsert_api_encryption_key], ids=["rewrite", "upsert"]
)
def test_default_neither_records_nor_drops_old_key(rewrite) -> None:
    out = rewrite(LIST_FORM.replace("    port", '      old_key: "older"\n    port'), NEW)
    assert out.endswith('    encryption:\n      old_key: "older"\n    port: 3232\n')
    assert "oldkey" not in out


def test_indirected_api_key_ignores_record_previous() -> None:
    yaml_text = LIST_FORM.replace('key: "oldkey"\n', "key: !secret api_key\n", 1)
    assert _rotate(yaml_text, NEW, record_previous=True) == yaml_text


def test_indirected_ota_key_still_raises_with_record_previous() -> None:
    yaml_text = API + BARE_OTA + "      key: !secret ota_key\n"
    with pytest.raises(YamlUpsertNotSupportedError, match="OTA encryption key"):
        _rotate(yaml_text, NEW, record_previous=True)


def test_multi_line_old_key_refuses_the_replace() -> None:
    yaml_text = BARE_ENCRYPTION + "      old_key: >-\n        older\n"
    with pytest.raises(YamlUpsertNotSupportedError, match="old_key spans more than one line"):
        _rotate(yaml_text, NEW, record_previous=True, replace_old_key=True)
    assert _rotate(yaml_text, NEW, record_previous=True).endswith(
        "      old_key: >-\n        older\n"
    )


def test_drop_ota_encryption_key_leaves_old_key_alone() -> None:
    out = drop_ota_encryption_key(LIST_FORM.replace("    port", '      old_key: "older"\n    port'))
    assert out.endswith('    encryption:\n      old_key: "older"\n    port: 3232\n')


@pytest.mark.parametrize(
    ("yaml_text", "expected"),
    [
        pytest.param(
            "ota:\n  - platform: esphome\n", "ota:\n  - platform: esphome\n", id="no-block"
        ),
        pytest.param(
            "ota:\r\n  - platform: esphome\r\n    encryption:\r\n"
            "      old_key: a\r\n    port: 1\r\n",
            "ota:\r\n  - platform: esphome\r\n    encryption:\r\n    port: 1\r\n",
            id="crlf",
        ),
        pytest.param(
            BARE_ENCRYPTION + "      old_key: a\n      old_key: b\n",
            BARE_ENCRYPTION,
            id="two-lines",
        ),
    ],
)
def test_drop_old_key(yaml_text: str, expected: str) -> None:
    assert drop_ota_encryption_old_key(yaml_text) == expected


def test_set_old_key_without_a_block_is_a_noop() -> None:
    yaml_text = "ota:\n  - platform: esphome"
    assert set_ota_encryption_old_key(yaml_text, "x") == yaml_text


def test_read_ota_encryption_key_ignores_old_key() -> None:
    assert read_ota_encryption_key(BARE_ENCRYPTION + '      old_key: "older"\n') is None


@pytest.mark.parametrize(
    ("yaml_text", "expected"),
    [
        pytest.param(BARE_ENCRYPTION + '      old_key: "older"\n', '"older"', id="list"),
        pytest.param(MAPPING_FORM + "    old_key: older\n", "older", id="mapping"),
        pytest.param(LIST_FORM, None, id="absent"),
        pytest.param(API, None, id="no-ota"),
    ],
)
def test_read_ota_encryption_old_key(yaml_text: str, expected: str | None) -> None:
    assert read_ota_encryption_old_key(yaml_text) == expected


@pytest.mark.parametrize(
    ("ota_tail", "expected"),
    [
        pytest.param('      old_key: "older"\n', True, id="old-key-differs"),
        pytest.param(f'      old_key: "{NEW}"\n', False, id="old-key-equals"),
        pytest.param('      key: "stale"\n      old_key: "older"\n', False, id="stale-key"),
        pytest.param("", True, id="bare"),
    ],
)
def test_api_key_settled_with_old_key(ota_tail: str, expected: bool) -> None:
    yaml_text = API.replace("oldkey", NEW) + BARE_OTA + ota_tail
    assert api_key_settled(yaml_text, NEW) is expected


@pytest.mark.parametrize(
    ("yaml_text", "expected"),
    [
        pytest.param(BARE_ENCRYPTION + OLD_KEY_LINE, True, id="block-child"),
        pytest.param(MAPPING_FORM + "    old_key: x\n", True, id="mapping-form"),
        pytest.param(
            "ota:\n  - platform: esphome\n    encryption: {old_key: x}\n", True, id="inline"
        ),
        pytest.param(
            "ota: {platform: esphome, encryption: {old_key: x}}\n", True, id="flow-header"
        ),
        pytest.param(
            'ota: {platform: esphome,\n  encryption: {old_key: "x"}}\n', True, id="flow-wrapped"
        ),
        pytest.param(BARE_ENCRYPTION + '      "old_key": x\n', True, id="quoted-child"),
        pytest.param(BARE_ENCRYPTION + "      # old_key: removed\n", False, id="comment"),
        pytest.param(
            BARE_ENCRYPTION + "    on_begin:\n      - lambda: 'old_key: text'\n",
            False,
            id="string-under-ota",
        ),
        pytest.param(
            BARE_ENCRYPTION
            + '    on_begin:\n      - lambda: |-\n          ESP_LOGD("t", "old_key: 1");\n',
            False,
            id="block-scalar-under-ota",
        ),
        pytest.param(
            "ota:\n  - platform: web_server\n    encryption:\n      old_key: x\n",
            False,
            id="other-platform",
        ),
        pytest.param("ota: !include common/ota.yaml\n", False, id="include-header"),
        pytest.param("ota: [\n", None, id="unparsable"),
        pytest.param(
            "packages:\n  base:\n    ota:\n      - platform: esphome\n        encryption:\n"
            "          old_key: x\n",
            True,
            id="inline-package",
        ),
        pytest.param("packages:\n  base: !include base.yaml\n", False, id="included-package"),
        pytest.param(
            "packages:\n  - ota:\n      - platform: esphome\n        encryption:\n"
            "          old_key: x\n",
            True,
            id="list-form-package",
        ),
        pytest.param(
            "packages:\n  outer:\n    packages:\n      inner:\n        ota:\n"
            "          - platform: esphome\n            encryption:\n              old_key: x\n",
            True,
            id="nested-package",
        ),
        pytest.param(API, False, id="no-ota"),
    ],
)
def test_yaml_declares_ota_old_key(yaml_text: str, expected: bool | None) -> None:
    assert yaml_declares_ota_old_key(yaml_text) is expected


@pytest.mark.parametrize(
    ("yaml_text", "record", "replace", "expected"),
    [
        pytest.param(BARE_ENCRYPTION, True, False, OldKeyOutcome.RECORDED, id="recorded"),
        pytest.param(BARE_ENCRYPTION, False, False, OldKeyOutcome.NOT_RECORDED, id="not-recorded"),
        pytest.param(
            API + "ota:\n  - platform: esphome\n",
            True,
            False,
            OldKeyOutcome.NO_ENCRYPTION_BLOCK,
            id="no-block",
        ),
        pytest.param(
            BARE_ENCRYPTION + '      old_key: "older"\n',
            True,
            False,
            OldKeyOutcome.KEPT_EXISTING,
            id="kept-existing",
        ),
        pytest.param(
            BARE_ENCRYPTION + '      old_key: "older"\n',
            True,
            True,
            OldKeyOutcome.RECORDED,
            id="replaced",
        ),
        pytest.param(
            BARE_ENCRYPTION + "      old_key: !secret previous\n",
            True,
            True,
            OldKeyOutcome.KEPT_INDIRECTED,
            id="kept-indirected",
        ),
        pytest.param(
            API.replace("oldkey", NEW) + BARE_OTA,
            True,
            False,
            OldKeyOutcome.NOT_NEEDED,
            id="same-key",
        ),
        pytest.param(
            LIST_FORM.replace('key: "oldkey"\n', "key: !secret api_key\n", 1),
            True,
            False,
            OldKeyOutcome.NOT_NEEDED,
            id="indirected-api-key",
        ),
    ],
)
def test_rotate_api_encryption_key_outcome(
    yaml_text: str, record: bool, replace: bool, expected: OldKeyOutcome
) -> None:
    _, outcome = rotate_api_encryption_key(
        yaml_text, NEW, record_previous=record, replace_old_key=replace
    )
    assert outcome is expected
