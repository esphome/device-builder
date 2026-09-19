"""``secret_redaction`` loads every secrets file and removes their values from output."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.secret_redaction import (
    load_secret_mappings,
    redact_secret_values,
)
from esphome_device_builder.models import ErrorCode


def test_loads_both_spellings_from_every_directory_once(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    (tmp_path / "secrets.yaml").write_text("wifi_password: hunter2xyz\n")
    (tmp_path / "secrets.yml").write_text("api_key: abcdefghij\n")
    (sub / "secrets.yaml").write_text("wifi_password: otherpass1\n")
    assert load_secret_mappings(sub, tmp_path, tmp_path) == [
        {"wifi_password": "otherpass1"},
        {"wifi_password": "hunter2xyz"},
        {"api_key": "abcdefghij"},
    ]


@pytest.mark.parametrize("content", ["", "# nothing yet\n"], ids=["empty", "comment_only"])
def test_an_empty_secrets_file_is_an_empty_mapping(tmp_path: Path, content: str) -> None:
    (tmp_path / "secrets.yaml").write_text(content)
    assert load_secret_mappings(tmp_path) == [{}]


def test_no_secrets_file_is_no_mappings(tmp_path: Path) -> None:
    assert load_secret_mappings(tmp_path) == []


@pytest.mark.parametrize(
    ("content", "problem"),
    [(b"- not\n- a mapping\n", "parsed"), (b"\xff\xfe not utf-8", "read")],
    ids=["not_a_mapping", "not_utf8"],
)
def test_an_unusable_secrets_file_is_unavailable(
    tmp_path: Path, content: bytes, problem: str
) -> None:
    (tmp_path / "secrets.yaml").write_bytes(content)
    with pytest.raises(CommandError) as excinfo:
        load_secret_mappings(tmp_path)
    assert excinfo.value.code is ErrorCode.UNAVAILABLE
    assert excinfo.value.message == f"secrets.yaml could not be {problem}"


def test_a_read_failure_is_unavailable(tmp_path: Path) -> None:
    with (
        patch.object(Path, "read_text", side_effect=PermissionError("denied")),
        pytest.raises(CommandError) as excinfo,
    ):
        load_secret_mappings(tmp_path)
    assert excinfo.value.message == "secrets.yaml could not be read"


def test_redacts_every_scalar_of_credential_length() -> None:
    mappings = [
        {"user": "alice_smith", "port": 1883, "flag": True, "nothing": None},
        {
            "user": "qwertyui",
            "nested": [{"token": "abcdefgh"}],
            "cert": "FIRSTLINEOFCERT\nSECONDLINE\n",
        },
    ]
    lines = [
        "user: alice_smith and qwertyui",
        "port: 1883 flag: True",
        "token: abcdefgh",
        "SECONDLINE",
    ]
    assert redact_secret_values(lines, mappings) == [
        "user: <removed> and <removed>",
        "port: 1883 flag: True",
        "token: <removed>",
        "<removed>",
    ]


def test_a_longer_value_is_removed_before_its_prefix() -> None:
    mappings = [{"a": "hunter2", "b": "hunter2-extended"}]
    assert redact_secret_values(["x hunter2-extended y"], mappings) == ["x <removed> y"]
