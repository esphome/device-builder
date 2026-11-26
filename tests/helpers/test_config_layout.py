"""Tests for ``helpers/config_layout.py``: which config-dir YAMLs are devices and packages."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.config_layout import (
    canonical_configuration,
    canonical_rel,
    config_relative,
    in_devices_dir,
    is_canonical_configuration,
    is_device_config_name,
    is_device_path,
    is_nested_device,
    is_package_path,
    is_secrets_like_name,
    iter_config_yamls,
    iter_device_files,
    iter_referrer_yamls,
    name_shared_by_another_device,
    refuse_nested_device,
    refuse_nested_name_clash,
    refuse_path_in_name,
    resolved_rel,
    sibling_secrets,
)
from esphome_device_builder.models import ErrorCode

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges")


def _write(root: Path, rel: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("a: 1\n", encoding="utf-8")


@pytest.mark.parametrize(
    "name",
    [
        "secrets.yaml",
        "secrets.yml",
        "secrets.ci.yaml",
        "SECRETS.Local.YML",
        "devices/garage/secrets.yaml",
        "common\\secrets.yaml",
        "secrets.yaml.",
    ],
)
def test_secrets_like_names(name: str) -> None:
    assert is_secrets_like_name(name)


@pytest.mark.parametrize(
    "name", ["secrets.json", "secretsx.yaml", "my-secrets.yaml", "kitchen.yaml"]
)
def test_not_secrets_like_names(name: str) -> None:
    assert not is_secrets_like_name(name)


@pytest.mark.parametrize(
    ("rel", "device", "package"),
    [
        ("kitchen.yaml", True, False),
        ("Kitchen.YML", True, False),
        ("devices/plug.yaml", True, False),
        ("devices/lights/desk.yaml", True, False),
        ("devices/a/b/c.yml", True, False),
        ("common/base.yaml", False, True),
        ("common/devices/x.yaml", False, True),
        # Folded like parse_file_path folds it, so files/* never lists what it refuses.
        ("Devices/x.yaml", False, False),
        ("archive/old.yaml", False, False),
        ("Archive/old.yaml", False, False),
        ("secrets.yaml", False, False),
        ("secrets.ci.yaml", False, False),
        ("devices/garage/secrets.yaml", False, False),
        ("common/secrets.local.yml", False, False),
        (".hidden.yaml", False, False),
        ("devices/.drafts/x.yaml", False, False),
        ("common/.x.yaml", False, False),
        ("devices//x.yaml", False, False),
        ("/x.yaml", False, False),
        ("devices/../x.yaml", False, False),
        ("devices/notes.txt", False, False),
        ("devices", False, False),
    ],
)
def test_device_and_package_paths(rel: str, device: bool, package: bool) -> None:
    assert is_device_path(rel) is device
    assert is_package_path(rel) is package


def test_in_devices_dir() -> None:
    assert in_devices_dir("devices/x.yaml")
    assert in_devices_dir("devices/a/x.txt")
    # A guard: every spelling that can reach a file below devices/ counts.
    assert in_devices_dir("./devices/lights/desk.yaml")
    assert in_devices_dir("common/../devices/lights/desk.yaml")
    assert in_devices_dir("devices//lights/desk.yaml")
    assert in_devices_dir("Devices/x.yaml")
    assert not in_devices_dir("devices")
    assert not in_devices_dir("./devices")
    assert not in_devices_dir("devices/../x.yaml")
    assert not in_devices_dir("../devices/x.yaml")
    assert not in_devices_dir("devices.yaml")
    assert not in_devices_dir("")
    assert not in_devices_dir("common/devices/x.yaml")


@pytest.mark.parametrize(
    "configuration",
    ["kitchen.yaml", ".hidden.yaml", "devices/lights/desk.yaml", "devices/a/b/c.yml", "notes.txt"],
)
def test_is_canonical_configuration_accepts(configuration: str) -> None:
    assert is_canonical_configuration(configuration)


@pytest.mark.parametrize(
    "configuration",
    [
        "",
        "/kitchen.yaml",
        "./kitchen.yaml",
        "x/../kitchen.yaml",
        "devices//desk.yaml",
        "devices/./desk.yaml",
        "devices/../kitchen.yaml",
        "devices/",
        "devices./desk.yaml",
        "devices /desk.yaml",
        "devices/lights./desk.yaml",
        "devices/lights /desk.yaml",
        "kitchen.yaml.",
        "kitchen.yaml ",
        "devices/li:ghts/desk.yaml",
        "C:kitchen.yaml",
        "devices/li\x00ghts/desk.yaml",
        "devices\\desk.yaml",
        "devices/.hidden/desk.yaml",
        "DEVICES/desk.yaml",
        "common/base.yaml",
    ],
)
def test_is_canonical_configuration_refuses(configuration: str) -> None:
    assert not is_canonical_configuration(configuration)


@posix_only
def test_resolved_rel(tmp_path: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    outside = tmp_path_factory.mktemp("outside")
    _write(tmp_path, "devices/lights/desk.yaml")
    (tmp_path / "link.yaml").symlink_to("devices/lights/desk.yaml")
    (tmp_path / "escape.yaml").symlink_to(outside / "evil.yaml")

    assert resolved_rel(tmp_path, "x/../devices/lights/desk.yaml") == "devices/lights/desk.yaml"
    assert resolved_rel(tmp_path, str(tmp_path / "link.yaml")) == "devices/lights/desk.yaml"
    assert resolved_rel(tmp_path, "new.yaml") == "new.yaml"
    assert resolved_rel(tmp_path, "escape.yaml") is None
    assert resolved_rel(tmp_path, "../x.yaml") is None


@posix_only
def test_canonical_configuration(tmp_path: Path) -> None:
    _write(tmp_path, "devices/lights/desk.yaml")
    (tmp_path / "lights").symlink_to("devices/lights", target_is_directory=True)
    (tmp_path / "link.yaml").symlink_to("devices/lights/desk.yaml")

    for spelling in (
        "devices/lights/desk.yaml",
        "./devices/lights/desk.yaml",
        "x/../devices/lights/desk.yaml",
        "devices//lights/desk.yaml",
        "devices/lights/desk.yaml/",
        "devices/lights/desk.yaml/.",
        str(tmp_path / "devices/lights/desk.yaml"),
        "lights/desk.yaml",
    ):
        assert canonical_configuration(tmp_path, spelling) == "devices/lights/desk.yaml"
        assert is_nested_device(tmp_path, spelling)
    # A top-level symlink is a device of its own; a new file maps like an existing one.
    assert canonical_configuration(tmp_path, "link.yaml") == "link.yaml"
    assert not is_nested_device(tmp_path, "link.yaml")
    assert canonical_configuration(tmp_path, "lights/new.yaml") == "devices/lights/new.yaml"
    # Outside the config dir: lexical only, for rel_path to refuse.
    assert canonical_configuration(tmp_path, "../devices/x.yaml") == "../devices/x.yaml"


def test_refuse_path_in_name() -> None:
    refuse_path_in_name("pantry")
    for name in ("devices/lights/pantry", "..\\pantry"):
        with pytest.raises(CommandError) as exc_info:
            refuse_path_in_name(name)
        assert exc_info.value.code is ErrorCode.INVALID_ARGS


def test_refuse_nested_device(tmp_path: Path) -> None:
    _write(tmp_path, "devices/lights/desk.yaml")

    for configuration in ("devices/lights/desk.yaml", "x/../devices/lights/desk.yaml"):
        with pytest.raises(CommandError) as exc_info:
            refuse_nested_device(tmp_path, configuration, "Renaming")
        assert exc_info.value.code is ErrorCode.INVALID_ARGS
        assert str(exc_info.value) == (
            "Renaming is not supported yet for a device inside the devices/ folder "
            f"({configuration})"
        )
    # A top-level name, or one escaping the config dir (rel_path refuses it), passes here.
    refuse_nested_device(tmp_path, "kitchen.yaml", "Renaming")
    refuse_nested_device(tmp_path, "../kitchen.yaml", "Renaming")


def test_canonical_rel() -> None:
    assert canonical_rel("./devices/lights/desk.yaml") == "devices/lights/desk.yaml"
    assert canonical_rel("a/../kitchen.yaml") == "kitchen.yaml"
    assert canonical_rel("kitchen.yaml") == "kitchen.yaml"


def test_config_relative(tmp_path: Path) -> None:
    assert config_relative(tmp_path, tmp_path / "kitchen.yaml") == "kitchen.yaml"
    assert config_relative(tmp_path, tmp_path / "devices/lights/desk.yaml") == (
        "devices/lights/desk.yaml"
    )
    for outside in (tmp_path, tmp_path.parent / "x.yaml", tmp_path.with_name("other") / "x.yaml"):
        with pytest.raises(ValueError, match="is not below"):
            config_relative(tmp_path, outside)


def test_sibling_secrets() -> None:
    assert sibling_secrets("kitchen.yaml") == "secrets.yaml"
    assert sibling_secrets("devices/lights/desk.yaml") == "devices/lights/secrets.yaml"


def test_name_shared_by_another_device(tmp_path: Path) -> None:
    for rel in ("lamp.yaml", "devices/a/lamp.yaml", "devices/b/desk.yaml", "common/desk.yaml"):
        _write(tmp_path, rel)

    assert name_shared_by_another_device(tmp_path, "lamp.yaml")
    assert name_shared_by_another_device(tmp_path, "./devices/a/lamp.yaml")
    # A package of the same name is not a device.
    assert not name_shared_by_another_device(tmp_path, "devices/b/desk.yaml")
    assert not name_shared_by_another_device(tmp_path, "devices/b/./desk.yaml")


def test_refuse_nested_name_clash(tmp_path: Path) -> None:
    for rel in ("kitchen.yaml", "devices/b/desk.yaml", "common/plug.yaml"):
        _write(tmp_path, rel)

    with pytest.raises(CommandError) as exc_info:
        refuse_nested_name_clash(tmp_path, "desk.yaml")

    assert exc_info.value.code is ErrorCode.INVALID_ARGS
    assert "(devices/b/desk.yaml)" in str(exc_info.value)
    # A top-level twin is the caller's own exists check; a package never clashes.
    refuse_nested_name_clash(tmp_path, "kitchen.yaml")
    refuse_nested_name_clash(tmp_path, "plug.yaml")


@pytest.mark.parametrize(
    "name",
    [
        "kitchen.yaml",
        "Kitchen.YML",
        "porch (1).yaml",
        "bedroom~1.yaml",
        "devices/lights/desk.yaml",
        "devices/plug.yml",
    ],
)
def test_is_device_config_name_accepts_device_paths(name: str) -> None:
    assert is_device_config_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "secrets.yaml",
        "SECRETS.YML",
        "secrets.ci.yaml",
        "secrets.yaml::$DATA",
        "SECRET~1.YAM",
        "notes.txt",
        "kitchen.yaml.",
        "kitchen.yaml ",
        "sub/kitchen.yaml",
        "sub\\kitchen.yaml",
        "devices\\kitchen.yaml",
        "devices/lights./desk.yaml",
        "devices/garage/secrets.yaml",
        "devices/x.yaml::$DATA",
        "../../etc/passwd.yaml",
        "devices/../../etc/passwd.yaml",
        "kitchen\x00.yaml",
        "config\\secrets.yaml",
        # Every segment is vetted, not only the file name.
        "devices/li:ghts/desk.yaml",
        "devices/li\x00ghts/desk.yaml",
        "devices/lights /desk.yaml",
        "devices./desk.yaml",
        "devices /desk.yaml",
        "devices/.hidden/desk.yaml",
        "devices//desk.yaml",
        "./kitchen.yaml",
    ],
)
def test_is_device_config_name_refuses_others(name: str) -> None:
    assert not is_device_config_name(name)


def test_iter_device_files_walks_top_level_and_devices_folder(tmp_path: Path) -> None:
    for rel in (
        "kitchen.yaml",
        "porch.yml",
        "secrets.yaml",
        "secrets.ci.yaml",
        ".hidden.yaml",
        "notes.txt",
        "devices/plug.yaml",
        "devices/lights/desk.yaml",
        "devices/garage/door.yaml",
        "devices/garage/secrets.yaml",
        "devices/.drafts/draft.yaml",
        "devices/lights/.x.yaml",
        "common/base.yaml",
        "archive/old.yaml",
        ".esphome/build/z.yaml",
    ):
        _write(tmp_path, rel)
    # A folder named like a YAML is not a device.
    (tmp_path / "folder.yaml").mkdir()

    assert iter_device_files(tmp_path) == [
        "devices/garage/door.yaml",
        "devices/lights/desk.yaml",
        "devices/plug.yaml",
        "kitchen.yaml",
        "porch.yml",
    ]


def test_iter_device_files_matches_the_devices_folder_name_exactly(tmp_path: Path) -> None:
    # Its own tree: beside devices/, a case-insensitive filesystem would put the file in it.
    _write(tmp_path, "Devices/upper.yaml")

    assert iter_device_files(tmp_path) == []


@posix_only
def test_iter_device_files_lists_past_a_symlink_loop(tmp_path: Path) -> None:
    # Python 3.12's Path.resolve raises RuntimeError for the loop, even when not strict.
    _write(tmp_path, "devices/plug.yaml")
    (tmp_path / "devices/loop.yaml").symlink_to("loop.yaml")

    assert "devices/plug.yaml" in iter_device_files(tmp_path)


@posix_only
def test_iter_device_files_symlinks(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("outside")
    _write(outside, "evil.yaml")
    _write(outside, "dir/deep.yaml")
    _write(tmp_path, "devices/real.yaml")
    (tmp_path / "devices/inside.yaml").symlink_to(tmp_path / "devices/real.yaml")
    (tmp_path / "devices/escape.yaml").symlink_to(outside / "evil.yaml")
    (tmp_path / "devices/linked").symlink_to(outside / "dir", target_is_directory=True)
    # A top-level symlink is taken as is, as esphome's own listing does.
    (tmp_path / "top.yaml").symlink_to(outside / "evil.yaml")

    assert iter_device_files(tmp_path) == ["devices/inside.yaml", "devices/real.yaml", "top.yaml"]


@posix_only
def test_iter_device_files_skips_a_symlinked_devices_folder(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("outside")
    _write(outside, "plug.yaml")
    (tmp_path / "devices").symlink_to(outside, target_is_directory=True)

    assert iter_device_files(tmp_path) == []


def test_iter_device_files_raises_when_the_config_dir_is_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        iter_device_files(tmp_path / "missing")


def test_iter_device_files_logs_a_subfolder_it_cannot_list(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write(tmp_path, "kitchen.yaml")
    _write(tmp_path, "devices/private/x.yaml")
    real_scandir = os.scandir

    def _scandir(path: Any) -> Any:
        if Path(path).name == "private":
            raise PermissionError(13, "denied", str(path))
        return real_scandir(path)

    with patch("os.scandir", _scandir):
        assert iter_device_files(tmp_path) == ["kitchen.yaml"]

    assert "private" in caplog.text


def test_iter_config_yamls_walks_everything_but_archive_and_hidden(tmp_path: Path) -> None:
    for rel in (
        "kitchen.yaml",
        "secrets.yaml",
        "devices/lights/desk.yaml",
        "common/base.yaml",
        "common/secrets.yaml",
        "common/archive/kept.yaml",
        "archive/old.yaml",
        ".git/x.yaml",
    ):
        _write(tmp_path, rel)

    assert sorted(iter_config_yamls(tmp_path)) == [
        "common/archive/kept.yaml",
        "common/base.yaml",
        "common/secrets.yaml",
        "devices/lights/desk.yaml",
        "kitchen.yaml",
        "secrets.yaml",
    ]


@posix_only
def test_iter_referrer_yamls_adds_hidden_files_but_no_hidden_folders(tmp_path: Path) -> None:
    for rel in (
        "kitchen.yaml",
        ".shared.yaml",
        "common/base.yaml",
        "common/.wrapper.yaml",
        "common/.cache/x.yaml",
        "archive/.old.yaml",
    ):
        _write(tmp_path, rel)
    outside = tmp_path.parent / f"{tmp_path.name}-outside.yaml"
    outside.write_text("a: 1\n", encoding="utf-8")
    # A hidden link is held inside the config dir at the top level too.
    (tmp_path / ".out.yaml").symlink_to(outside)
    (tmp_path / ".in.yaml").symlink_to("kitchen.yaml")

    assert sorted(iter_referrer_yamls(tmp_path)) == [
        ".in.yaml",
        ".shared.yaml",
        "common/.wrapper.yaml",
        "common/base.yaml",
        "kitchen.yaml",
    ]
    # The device and package walks are unchanged.
    assert sorted(iter_config_yamls(tmp_path)) == ["common/base.yaml", "kitchen.yaml"]
