"""Tests for the esphome OTA platform ``encryption.key`` reader / rewriter."""

from __future__ import annotations

import pytest

from esphome_device_builder.helpers.yaml import (
    read_ota_encryption_key,
    rewrite_ota_encryption_key,
)

NEW = "bmV3a2V5bmV3a2V5bmV3a2V5bmV3a2V5bmV3a2V5bmV3a2V5bmU="

LIST_FORM = """\
api:
  encryption:
    key: "oldkey"

ota:
  - platform: esphome
    encryption:
      key: "oldkey"  # keep me
    port: 3232
"""

MAPPING_FORM = """\
ota:
  platform: esphome
  encryption:
    key: oldkey
"""

BARE_MAPPING_FORM = """\
ota:
  encryption:
    key: oldkey
"""

SECOND_ITEM = """\
ota:
  - platform: web_server
  - platform: esphome
    password: hunter2
    encryption:
      key: 'oldkey'
"""

BARE_ENCRYPTION = """\
ota:
  - platform: esphome
    encryption:
"""

OTHER_PLATFORM_ONLY = """\
ota:
  - platform: web_server
    encryption:
      key: oldkey
"""


@pytest.mark.parametrize(
    ("yaml_text", "expected"),
    [
        pytest.param(LIST_FORM, '"oldkey"', id="list"),
        pytest.param(MAPPING_FORM, "oldkey", id="mapping"),
        pytest.param(BARE_MAPPING_FORM, "oldkey", id="mapping-no-platform"),
        pytest.param(SECOND_ITEM, "'oldkey'", id="second-item"),
        pytest.param(BARE_ENCRYPTION, None, id="bare-encryption"),
        pytest.param(OTHER_PLATFORM_ONLY, None, id="other-platform"),
        pytest.param("ota:\n  - platform: esphome\n    password: x\n", None, id="no-encryption"),
        pytest.param("api:\n  encryption:\n    key: oldkey\n", None, id="no-ota"),
        pytest.param("", None, id="empty"),
    ],
)
def test_read_ota_encryption_key(yaml_text: str, expected: str | None) -> None:
    assert read_ota_encryption_key(yaml_text) == expected


def test_rewrite_list_form_keeps_comment_and_siblings() -> None:
    out = rewrite_ota_encryption_key(LIST_FORM, NEW)
    assert f'      key: "{NEW}"  # keep me\n' in out
    assert 'api:\n  encryption:\n    key: "oldkey"' in out
    assert "    port: 3232\n" in out
    assert read_ota_encryption_key(out) == f'"{NEW}"'


def test_rewrite_mapping_form() -> None:
    out = rewrite_ota_encryption_key(MAPPING_FORM, NEW)
    assert out == f'ota:\n  platform: esphome\n  encryption:\n    key: "{NEW}"\n'


def test_rewrite_skips_non_esphome_item() -> None:
    out = rewrite_ota_encryption_key(SECOND_ITEM, NEW)
    assert "  - platform: web_server\n" in out
    assert f'      key: "{NEW}"\n' in out
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
def test_rewrite_leaves_indirection_alone(value: str) -> None:
    yaml_text = f"ota:\n  - platform: esphome\n    encryption:\n      key: {value}\n"
    assert rewrite_ota_encryption_key(yaml_text, NEW) == yaml_text


@pytest.mark.parametrize(
    "yaml_text",
    [
        pytest.param(BARE_ENCRYPTION, id="bare-encryption"),
        pytest.param(OTHER_PLATFORM_ONLY, id="other-platform"),
        pytest.param("esphome:\n  name: x\n", id="no-ota"),
    ],
)
def test_rewrite_no_key_is_noop(yaml_text: str) -> None:
    assert rewrite_ota_encryption_key(yaml_text, NEW) == yaml_text


def test_rewrite_preserves_crlf() -> None:
    yaml_text = "ota:\r\n  - platform: esphome\r\n    encryption:\r\n      key: oldkey\r\n"
    out = rewrite_ota_encryption_key(yaml_text, NEW)
    assert out == f'ota:\r\n  - platform: esphome\r\n    encryption:\r\n      key: "{NEW}"\r\n'
