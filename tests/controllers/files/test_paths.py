"""Tests for ``controllers/files/paths.py``: wire-path validation and package discovery."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from esphome_device_builder.controllers.files.paths import list_package_files, parse_file_path
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges")


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("common/base.yaml", "common/base.yaml"),
        ("common//sub/x.yml", "common/sub/x.yml"),
        ("common/archive/x.yaml", "common/archive/x.yaml"),
        ("Common/Base.YAML", "Common/Base.YAML"),
    ],
)
def test_parse_accepts_subfolder_yaml(path: str, expected: str) -> None:
    assert parse_file_path(path, in_subfolder=True) == expected


@pytest.mark.parametrize(
    "path",
    [
        None,
        "",
        42,
        "kitchen.yaml",
        "/etc/x.yaml",
        "../x.yaml",
        "common/../../x.yaml",
        ".hidden/x.yaml",
        "common/.x.yaml",
        ".esphome/build/x.yaml",
        "common/x.txt",
        "common/x",
        "common/secrets.yaml",
        "common/SECRETS.yml",
        "archive/old.yaml",
        "Archive/old.yaml",
        "common\\x.yaml",
        "C:/x.yaml",
        "common/x.yaml:stream",
        "common/x\x00.yaml",
    ],
)
def test_parse_refuses(path: object) -> None:
    with pytest.raises(CommandError) as exc_info:
        parse_file_path(path, in_subfolder=True)
    assert exc_info.value.code is ErrorCode.INVALID_ARGS


def test_parse_top_level_allowed_outside_subfolder_mode() -> None:
    assert parse_file_path("kitchen.yaml", in_subfolder=False) == "kitchen.yaml"
    with pytest.raises(CommandError):
        parse_file_path("secrets.yaml", in_subfolder=False)


@pytest.mark.parametrize(
    "name", ["base 1", "base#1", "it's", "a{b}", "a[1]", "a,b", "@main", "!x", "&a", "*a", "-a"]
)
def test_parse_new_file_refuses_names_that_need_yaml_quoting(name: str) -> None:
    path = f"common/{name}.yaml"
    assert parse_file_path(path, in_subfolder=True) == path  # existing files stay usable
    with pytest.raises(CommandError):
        parse_file_path(path, in_subfolder=True, plain_name=True)


def test_parse_plain_name_checks_every_segment() -> None:
    assert parse_file_path("common/_base.yaml", in_subfolder=True, plain_name=True)
    with pytest.raises(CommandError):
        parse_file_path("@common/base.yaml", in_subfolder=True, plain_name=True)


def _write(root: Path, rel: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("a: 1\n", encoding="utf-8")


def test_list_package_files_applies_skip_rules(tmp_path: Path) -> None:
    for rel in (
        "kitchen.yaml",
        "common/base.yaml",
        "common/sub/x.yml",
        "common/archive/kept.yaml",
        "common/secrets.yaml",
        "common/.hidden.yaml",
        "common/notes.txt",
        "archive/old.yaml",
        ".esphome/build/z.yaml",
        ".git/x.yaml",
    ):
        _write(tmp_path, rel)

    assert list_package_files(tmp_path) == [
        "common/archive/kept.yaml",
        "common/base.yaml",
        "common/sub/x.yml",
    ]


@posix_only
def test_list_package_files_symlinks(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("outside")
    _write(outside, "evil.yaml")
    _write(outside, "dir/deep.yaml")
    _write(tmp_path, "common/base.yaml")
    (tmp_path / "common/inside.yaml").symlink_to(tmp_path / "common/base.yaml")
    (tmp_path / "common/escape.yaml").symlink_to(outside / "evil.yaml")
    (tmp_path / "linked").symlink_to(outside / "dir", target_is_directory=True)

    assert list_package_files(tmp_path) == ["common/base.yaml", "common/inside.yaml"]


def test_list_package_files_empty_config_dir(tmp_path: Path) -> None:
    assert list_package_files(tmp_path) == []
