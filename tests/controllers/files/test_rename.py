"""Tests for ``files/rename`` and the include rewriting behind it."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from esphome import yaml_util

from esphome_device_builder.controllers.devices import archive, file_change
from esphome_device_builder.controllers.files import FilesController
from esphome_device_builder.controllers.files import controller as controller_module
from esphome_device_builder.controllers.files.rename import (
    find_referrers,
    plan_rename,
    rewrite_includes,
)
from esphome_device_builder.controllers.version_history import VersionHistoryController
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.async_ import run_in_executor
from esphome_device_builder.helpers.event_bus import EventBus
from esphome_device_builder.models import ErrorCode, UserPreferences

# Captured before the ``setup`` fixture stubs it out.
_real_commit_history_many = file_change._commit_history_many


@pytest.mark.parametrize(
    ("text", "folder", "expected"),
    [
        # a device at the top level
        ("base: !include common/base.yaml\n", "", "base: !include common/main.yaml\n"),
        ('base: !include "common/base.yaml"\n', "", 'base: !include "common/main.yaml"\n'),
        (
            "base: !include {file: common/base.yaml, vars: {a: 1}}\n",
            "",
            "base: !include {file: common/main.yaml, vars: {a: 1}}\n",
        ),
        # block form: the path sits under ``file:`` on the next, deeper line
        (
            "b: !include\n  file: common/base.yaml\n  vars: {a: 1}\n",
            "",
            "b: !include\n  file: common/main.yaml\n  vars: {a: 1}\n",
        ),
        ("b: !include\nfile: common/base.yaml\n", "", "b: !include\nfile: common/base.yaml\n"),
        # a sibling package refers to it relative to its own folder
        ("<<: !include ./base.yaml\n", "common", "<<: !include ./main.yaml\n"),
        # untouched: other files, comments, substitutions, strings, longer names
        ("x: !include common/other.yaml\n", "", "x: !include common/other.yaml\n"),
        ("# x: !include common/base.yaml\n", "", "# x: !include common/base.yaml\n"),
        ("x: !include ${d}/base.yaml\n", "", "x: !include ${d}/base.yaml\n"),
        ('note: "!include common/base.yaml"\n', "", 'note: "!include common/base.yaml"\n'),
        ('m: "Use !include common/base.yaml"\n', "", 'm: "Use !include common/base.yaml"\n'),
        ("m: 'Use !include common/base.yaml'\n", "", "m: 'Use !include common/base.yaml'\n"),
        ("x: !include common/base.yaml.bak\n", "", "x: !include common/base.yaml.bak\n"),
        ("x: !include /cfg/common/base.yaml\n", "", "x: !include /cfg/common/base.yaml\n"),
        # a quoted ``#`` is not a comment
        (
            'packages: {"base #1": !include common/base.yaml}\n',
            "",
            'packages: {"base #1": !include common/main.yaml}\n',
        ),
        # the include mapping's own ``file`` key in any order; a nested ``vars.file`` is not it
        (
            "b: !include\n  vars: {a: 1}\n  file: common/base.yaml\n",
            "",
            "b: !include\n  vars: {a: 1}\n  file: common/main.yaml\n",
        ),
        (
            "b: !include {vars: {a: 1}, file: common/base.yaml}\n",
            "",
            "b: !include {vars: {a: 1}, file: common/main.yaml}\n",
        ),
        (
            'b: !include {vars: {n: "a, file: x"}, file: "common/base.yaml"}\n',
            "",
            'b: !include {vars: {n: "a, file: x"}, file: "common/main.yaml"}\n',
        ),
        (
            "b: !include\n  file: common/other.yaml\n  vars:\n    file: common/base.yaml\n",
            "",
            "b: !include\n  file: common/other.yaml\n  vars:\n    file: common/base.yaml\n",
        ),
        (
            "b: !include {file: common/other.yaml, vars: {file: common/base.yaml}}\n",
            "",
            "b: !include {file: common/other.yaml, vars: {file: common/base.yaml}}\n",
        ),
        (
            "p: {a: !include common/base.yaml, b: !include common/base.yaml}\n",
            "",
            "p: {a: !include common/main.yaml, b: !include common/main.yaml}\n",
        ),
        # quoted variables, escapes included, are skipped whole
        (
            'b: !include {vars: {text: "a \\" b"}, file: common/base.yaml}\n',
            "",
            'b: !include {vars: {text: "a \\" b"}, file: common/main.yaml}\n',
        ),
        (
            "b: !include {vars: {text: 'it''s, file: x'}, file: common/base.yaml}\n",
            "",
            "b: !include {vars: {text: 'it''s, file: x'}, file: common/main.yaml}\n",
        ),
        # a nested block include keeps the outer one's ``file`` key reachable
        (
            (
                "b: !include\n  vars:\n    inner: !include\n      file: common/other.yaml\n"
                "  file: common/base.yaml\n"
            ),
            "",
            (
                "b: !include\n  vars:\n    inner: !include\n      file: common/other.yaml\n"
                "  file: common/main.yaml\n"
            ),
        ),
    ],
)
def test_rewrite_includes(text: str, folder: str, expected: str) -> None:
    rewritten = rewrite_includes(
        text, folder=folder, old="common/base.yaml", new="common/main.yaml"
    )
    assert rewritten == expected


def _tree(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))  # no CRLF on Windows


def test_plan_rename_covers_devices_and_packages(tmp_path: Path) -> None:
    _tree(
        tmp_path,
        {
            "kitchen.yaml": "packages:\n  base: !include common/base.yaml\n",
            "garage.yaml": "packages:\n  base: !include ${dir}/base.yaml\n",
            "named.yaml": "packages:\n  base: !include common/${name}.yaml\n",
            "other.yaml": "logger:  # base.yaml\n",
            "secrets.yaml": "x: !include common/base.yaml\n",
            "common/base.yaml": "wifi: !include wifi.yaml\n",
            "common/extra.yaml": "<<: !include base.yaml\n",
        },
    )

    plan = plan_rename(tmp_path, "common/base.yaml", "common/main.yaml")

    assert plan.texts == {
        "kitchen.yaml": "packages:\n  base: !include common/main.yaml\n",
        "common/extra.yaml": "<<: !include main.yaml\n",
    }
    assert plan.originals["kitchen.yaml"] == "packages:\n  base: !include common/base.yaml\n"
    assert plan.remaining == {"garage.yaml", "named.yaml", "secrets.yaml"}


@pytest.fixture
def files(tmp_path: Path, setup: tuple[FilesController, object, list]) -> FilesController:
    controller, _, _ = setup
    _tree(
        tmp_path,
        {
            "kitchen.yaml": "packages:\n  base: !include common/base.yaml\n",
            "garage.yaml": "packages:\n  base: !include ${dir}/base.yaml\n",
            "common/base.yaml": "wifi: !include wifi.yaml\n",
            "common/wifi.yaml": "ssid: x\n",
        },
    )
    return controller


def _users() -> list[SimpleNamespace]:
    deps = ("common/base.yaml", "common/wifi.yaml")
    return [
        SimpleNamespace(configuration="kitchen.yaml", dependencies=deps),
        SimpleNamespace(configuration="garage.yaml", dependencies=deps),
    ]


def test_plan_rename_matches_an_uppercase_extension(tmp_path: Path) -> None:
    _tree(tmp_path, {"kitchen.yaml": "base: !include common/Base.YAML\n"})

    plan = plan_rename(tmp_path, "common/Base.YAML", "common/Main.YAML")

    assert plan.texts == {"kitchen.yaml": "base: !include common/Main.YAML\n"}
    assert plan.remaining == set()


def test_plan_rename_checks_secrets_and_block_form_substitutions(tmp_path: Path) -> None:
    _tree(
        tmp_path,
        {
            "kitchen.yaml": "packages:\n  b: !include\n    file: ${dir}/base.yaml\n",
            "secrets.yaml": "<<: !include common/base.yaml\n",
        },
    )

    plan = plan_rename(tmp_path, "common/base.yaml", "common/main.yaml")

    # flagged, but a secrets file is never rewritten
    assert plan.texts == {}
    assert plan.remaining == {"kitchen.yaml", "secrets.yaml"}


def test_plan_rename_skips_a_file_that_is_not_utf8(tmp_path: Path) -> None:
    _tree(tmp_path, {"kitchen.yaml": "base: !include common/base.yaml\n"})
    (tmp_path / "binary.yaml").write_bytes(b"\xff\xfe!include common/base.yaml\n")

    plan = plan_rename(tmp_path, "common/base.yaml", "common/main.yaml")

    assert set(plan.texts) == {"kitchen.yaml"}
    assert plan.remaining == set()


def test_plan_rename_keeps_crlf_line_endings(tmp_path: Path) -> None:
    (tmp_path / "kitchen.yaml").write_bytes(b"packages:\r\n  base: !include common/base.yaml\r\n")

    plan = plan_rename(tmp_path, "common/base.yaml", "common/main.yaml")

    assert plan.texts["kitchen.yaml"] == "packages:\r\n  base: !include common/main.yaml\r\n"
    assert plan.originals["kitchen.yaml"].count("\r\n") == 2


def test_plan_rename_rewrites_archived_devices_from_the_config_dir(tmp_path: Path) -> None:
    _tree(tmp_path, {"archive/old.yaml": "packages:\n  base: !include common/base.yaml\n"})

    plan = plan_rename(tmp_path, "common/base.yaml", "common/main.yaml")

    assert plan.texts == {"archive/old.yaml": "packages:\n  base: !include common/main.yaml\n"}


def test_plan_rename_leaves_skipped_files_alone(tmp_path: Path) -> None:
    _tree(
        tmp_path,
        {
            "kitchen.yaml": "base: !include common/base.yaml\n",
            "archive/old.yaml": "base: !include common/base.yaml\n",
            "archive/dyn.yaml": "base: !include ${d}/base.yaml\n",
        },
    )

    plan = plan_rename(
        tmp_path,
        "common/base.yaml",
        "common/main.yaml",
        frozenset({"archive/old.yaml", "archive/dyn.yaml"}),
    )

    assert set(plan.texts) == {"kitchen.yaml"}
    assert plan.remaining == set()


@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        # a font / image ``file:`` is not an include
        (
            'hw: !include common/base.yaml\nfont:\n  - file: "fonts/${f}.ttf"\n    id: f1\n',
            False,
        ),
        ("hw: !include\n  file: common/base.yaml\n  vars:\n    file: ${x}\n", False),
        ('hw: !include {file: common/base.yaml, vars: {file: "${x}"}}\n', False),
        # a substitution-built target, whatever the key order
        ("hw: !include\n  vars: {a: 1}\n  file: ${d}/base.yaml\n", True),
        ("hw: !include {vars: {a: 1}, file: ${d}/base.yaml}\n", True),
        # a flow mapping continuing on the next line can't be read, so it is flagged
        ('hw: !include {\n  file: "common/${name}.yaml"\n}\n', True),
    ],
)
def test_plan_rename_flags_only_substitution_built_include_targets(
    tmp_path: Path, text: str, flagged: bool
) -> None:
    _tree(tmp_path, {"kitchen.yaml": text})

    plan = plan_rename(tmp_path, "common/base.yaml", "common/main.yaml")

    assert plan.remaining == ({"kitchen.yaml"} if flagged else set())


# Another file whose name starts with the target's, or the target named only
# inside a variable's string; the loader includes something else in each.
_LOOKALIKES = [
    "b: !include common/base.yaml#backup.yaml\n",
    "b: !include common/base.yaml[backup].yaml\n",
    (
        'b: !include {vars: {text: "a \\"}, file: common/base.yaml, x: \\""},'
        " file: common/other.yaml}\n"
    ),
    # a plain scalar continuing on the next line names ``common/base.yaml suffix.yaml``
    "b: !include common/base.yaml\n  suffix.yaml\n",
    "b: !include {file: common/base.yaml\n    suffix.yaml}\n",
]


@pytest.mark.parametrize("text", _LOOKALIKES)
def test_lookalike_includes_are_left_alone(tmp_path: Path, text: str) -> None:
    _tree(tmp_path, {"kitchen.yaml": text})

    assert rewrite_includes(text, folder="", old="common/base.yaml", new="common/main.yaml") == (
        text
    )
    assert find_referrers(tmp_path, "common/base.yaml") == ([], [])


@pytest.mark.parametrize(
    ("text", "lookalike"),
    [
        (_LOOKALIKES[0], "common/base.yaml#backup.yaml"),
        (_LOOKALIKES[1], "common/base.yaml[backup].yaml"),
        (_LOOKALIKES[3], "common/base.yaml suffix.yaml"),
        (_LOOKALIKES[4], "common/base.yaml suffix.yaml"),
    ],
)
def test_rename_keeps_a_lookalike_include_loadable(
    tmp_path: Path, text: str, lookalike: str
) -> None:
    _tree(
        tmp_path,
        {
            "kitchen.yaml": text,
            "common/base.yaml": "a: base\n",
            lookalike: "a: lookalike\n",
        },
    )

    plan = plan_rename(tmp_path, "common/base.yaml", "common/main.yaml")
    (tmp_path / "common/base.yaml").rename(tmp_path / "common/main.yaml")

    assert plan.texts == {}
    assert yaml_util.load_yaml(tmp_path / "kitchen.yaml")["b"].load() == {"a": "lookalike"}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "packages: {\n  a: !include base.yaml,\n  b: !include other.yaml\n}\n",
            "packages: {\n  a: !include main.yaml,\n  b: !include other.yaml\n}\n",
        ),
        (
            "packages: {\n  b: !include other.yaml,\n  a: !include base.yaml\n}\n",
            "packages: {\n  b: !include other.yaml,\n  a: !include main.yaml\n}\n",
        ),
        (
            "packages: [\n  !include base.yaml,\n  !include other.yaml\n]\n",
            "packages: [\n  !include main.yaml,\n  !include other.yaml\n]\n",
        ),
        (
            "packages: [\n  !include other.yaml,\n  !include base.yaml]\n",
            "packages: [\n  !include other.yaml,\n  !include main.yaml]\n",
        ),
        # a single-line include followed by another key still rewrites
        (
            "a: !include base.yaml\nb: 1\n",
            "a: !include main.yaml\nb: 1\n",
        ),
    ],
)
def test_includes_inside_multiline_flow_collections(
    tmp_path: Path, text: str, expected: str
) -> None:
    _tree(tmp_path, {"common/wrapper.yaml": text})

    assert rewrite_includes(
        text, folder="common", old="common/base.yaml", new="common/main.yaml"
    ) == (expected)
    assert find_referrers(tmp_path, "common/base.yaml").rewritable == ["common/wrapper.yaml"]


def test_nested_block_includes_are_found_independently(tmp_path: Path) -> None:
    _tree(
        tmp_path,
        {
            "kitchen.yaml": (
                "b: !include\n  vars:\n    inner: !include\n      file: common/other.yaml\n"
                "  file: common/base.yaml\n"
            ),
        },
    )

    assert find_referrers(tmp_path, "common/base.yaml").rewritable == ["kitchen.yaml"]
    assert find_referrers(tmp_path, "common/other.yaml").rewritable == ["kitchen.yaml"]
    plan = plan_rename(tmp_path, "common/other.yaml", "common/main.yaml")
    assert plan.texts["kitchen.yaml"].count("common/main.yaml") == 1
    assert "file: common/base.yaml" in plan.texts["kitchen.yaml"]


_REFERRER_TREE = {
    "kitchen.yaml": "packages:\n  base: !include common/base.yaml\n",
    "garage.yaml": "packages:\n  base: !include ${dir}/base.yaml\n",
    "quoted.yaml": 'note: "!include common/base.yaml"\n',
    "secrets.yaml": "x: !include common/base.yaml\n",
    "common/extra.yaml": "<<: !include base.yaml\n",
    "bridge/plug.yaml": "b: !include\n  vars: {a: 1}\n  file: ../common/base.yaml\n",
    "archive/old.yaml": "b: !include {vars: {a: 1}, file: common/base.yaml}\n",
    "archive/other.yaml": "b: !include common/other.yaml\n",
}


def test_find_referrers(tmp_path: Path) -> None:
    _tree(tmp_path, _REFERRER_TREE)

    assert find_referrers(tmp_path, "common/base.yaml").referenced_by == [
        "archive/old.yaml",
        "bridge/plug.yaml",
        "common/extra.yaml",
        "kitchen.yaml",
    ]


@pytest.mark.parametrize("path", ["common/base.yaml", "common/other.yaml", "bridge/plug.yaml"])
def test_find_referrers_matches_what_a_rename_rewrites(tmp_path: Path, path: str) -> None:
    _tree(tmp_path, _REFERRER_TREE)

    plan = plan_rename(tmp_path, path, path.replace(".yaml", "-new.yaml"))

    referrers = find_referrers(tmp_path, path)

    assert referrers.rewritable == referrers.referenced_by == sorted(plan.texts)


async def test_rename_renames_rewrites_and_reports(
    tmp_path: Path, files: FilesController, setup: tuple[FilesController, object, list]
) -> None:
    _, devices, commits = setup

    with patch.object(devices, "get_devices", return_value=_users()):
        result = await files.rename(path="common/base.yaml", new_path="common/main.yaml")

    assert result == {
        "path": "common/main.yaml",
        "rewritten": ["kitchen.yaml"],
        "unresolved": ["garage.yaml"],
    }
    assert not (tmp_path / "common/base.yaml").exists()
    assert (tmp_path / "common/main.yaml").read_text(encoding="utf-8") == (
        "wifi: !include wifi.yaml\n"
    )
    assert "common/main.yaml" in (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    # one history commit covering every touched file
    assert {c for c, _ in commits} == {"common/base.yaml", "common/main.yaml", "kitchen.yaml"}
    assert {m for _, m in commits} == {"Rename common/base.yaml to common/main.yaml"}


@pytest.mark.parametrize(
    ("new_path", "code"),
    [
        ("common/wifi.yaml", ErrorCode.ALREADY_EXISTS),
        ("common/base.yaml", ErrorCode.INVALID_ARGS),
        ("shared/base.yaml", ErrorCode.INVALID_ARGS),
        ("common/sub/base.yaml", ErrorCode.INVALID_ARGS),
        ("base.yaml", ErrorCode.INVALID_ARGS),
        ("../x/base.yaml", ErrorCode.INVALID_ARGS),
        ("common/base #1.yaml", ErrorCode.INVALID_ARGS),
        ("common/@main.yaml", ErrorCode.INVALID_ARGS),
    ],
)
async def test_rename_refusals_change_nothing(
    tmp_path: Path,
    files: FilesController,
    setup: tuple[FilesController, object, list],
    new_path: str,
    code: ErrorCode,
) -> None:
    _, devices, commits = setup
    before = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")

    with (
        patch.object(devices, "get_devices", return_value=_users()),
        pytest.raises(CommandError) as exc_info,
    ):
        await files.rename(path="common/base.yaml", new_path=new_path)

    assert exc_info.value.code is code
    assert (tmp_path / "common/base.yaml").exists()
    assert (tmp_path / "kitchen.yaml").read_text(encoding="utf-8") == before
    assert commits == []


async def test_rename_refuses_a_source_name_that_needs_quoting(
    tmp_path: Path, files: FilesController
) -> None:
    _tree(tmp_path, {"common/base #1.yaml": "a: 1\n"})
    with pytest.raises(CommandError) as exc_info:
        await files.rename(path="common/base #1.yaml", new_path="common/main.yaml")
    assert exc_info.value.code is ErrorCode.INVALID_ARGS
    assert (tmp_path / "common/base #1.yaml").exists()


async def test_rename_missing_source(files: FilesController) -> None:
    with pytest.raises(CommandError) as exc_info:
        await files.rename(path="common/nope.yaml", new_path="common/new.yaml")
    assert exc_info.value.code is ErrorCode.NOT_FOUND


async def test_rename_flags_a_device_with_a_missed_reference_despite_a_rewrite(
    tmp_path: Path, files: FilesController, setup: tuple[FilesController, object, list]
) -> None:
    _, devices, _ = setup
    _tree(
        tmp_path,
        {
            "garage.yaml": (
                "packages:\n  a: !include common/base.yaml\n  b: !include common/${name}.yaml\n"
            ),
        },
    )
    users = [SimpleNamespace(configuration="garage.yaml", dependencies=("common/base.yaml",))]

    with patch.object(devices, "get_devices", return_value=users):
        result = await files.rename(path="common/base.yaml", new_path="common/main.yaml")

    assert "garage.yaml" in result["rewritten"]
    assert result["unresolved"] == ["garage.yaml"]


async def _real_history(devices: Any, monkeypatch: pytest.MonkeyPatch) -> VersionHistoryController:
    """Attach a started, real version history; its baseline holds the devices only."""
    monkeypatch.setattr(file_change, "_commit_history_many", _real_commit_history_many)
    prefs = UserPreferences(version_history_enabled=True)
    history = VersionHistoryController(
        SimpleNamespace(  # type: ignore[arg-type]
            bus=EventBus(),
            devices=devices,
            settings=devices._db.settings,
            config=SimpleNamespace(prefs=SimpleNamespace(snapshot=lambda: prefs)),
        )
    )
    await history.start()
    devices._db.version_history = history
    return history


async def test_rename_records_one_real_history_commit(
    tmp_path: Path,
    files: FilesController,
    setup: tuple[FilesController, Any, list],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, devices, _ = setup
    _tree(tmp_path, {"archive/old.yaml": "base: !include common/base.yaml\n"})
    history = await _real_history(devices, monkeypatch)

    with patch.object(devices, "get_devices", return_value=_users()):
        await files.rename(path="common/base.yaml", new_path="common/main.yaml")
    await history.stop()

    repo = history._repo
    # the untracked package was recorded first
    base_log = await run_in_executor(repo.log_file, tmp_path / "common/base.yaml")
    assert [c.message for c in base_log] == [
        "Rename common/base.yaml to common/main.yaml",
        "Before: Rename common/base.yaml to common/main.yaml",
    ]
    # then one commit: the old path removed, the new one added, the referrer rewritten
    heads = {
        rel: (await run_in_executor(repo.log_file, tmp_path / rel))[0]
        for rel in ("common/base.yaml", "common/main.yaml", "kitchen.yaml", "archive/old.yaml")
    }
    assert {c.message for c in heads.values()} == {"Rename common/base.yaml to common/main.yaml"}
    assert len({c.sha for c in heads.values()}) == 1
    sha = heads["kitchen.yaml"].sha
    assert await run_in_executor(repo.file_at, tmp_path / "common/base.yaml", sha) is None
    main = await run_in_executor(repo.file_at, tmp_path / "common/main.yaml", sha)
    assert main == "wifi: !include wifi.yaml\n"


async def test_delete_keeps_an_untracked_package_in_history(
    tmp_path: Path,
    files: FilesController,
    setup: tuple[FilesController, Any, list],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, devices, _ = setup
    history = await _real_history(devices, monkeypatch)

    await files.delete(path="common/wifi.yaml")
    await history.stop()

    assert not (tmp_path / "common/wifi.yaml").exists()
    latest = await run_in_executor(history._repo.latest_content, tmp_path / "common/wifi.yaml")
    assert latest is not None
    assert latest[1] == "ssid: x\n"


async def test_rename_failed_move_restores_the_referrers(
    tmp_path: Path, files: FilesController, setup: tuple[FilesController, object, list]
) -> None:
    _, devices, commits = setup
    before = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")

    with (
        patch.object(devices, "get_devices", return_value=_users()),
        patch.object(Path, "rename", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        await files.rename(path="common/base.yaml", new_path="common/main.yaml")

    assert (tmp_path / "kitchen.yaml").read_text(encoding="utf-8") == before
    assert (tmp_path / "common/base.yaml").exists()
    assert not (tmp_path / "common/main.yaml").exists()
    assert commits == []


async def test_rename_refuses_when_a_new_referrer_appears_before_the_locks(
    tmp_path: Path, files: FilesController, setup: tuple[FilesController, object, list]
) -> None:
    _, _, commits = setup
    calls = 0

    def _plan_then_add_referrer(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        plan = plan_rename(*args, **kwargs)
        if calls == 1:
            _tree(tmp_path, {"late.yaml": "packages:\n  b: !include common/base.yaml\n"})
        return plan

    with (
        patch(f"{controller_module.__name__}.plan_rename", _plan_then_add_referrer),
        pytest.raises(CommandError) as exc_info,
    ):
        await files.rename(path="common/base.yaml", new_path="common/main.yaml")

    assert exc_info.value.code is ErrorCode.PRECONDITION_FAILED
    assert (tmp_path / "common/base.yaml").exists()
    assert commits == []


async def test_rename_skips_the_files_the_user_unticked(
    tmp_path: Path, files: FilesController, setup: tuple[FilesController, object, list]
) -> None:
    _, devices, commits = setup
    _tree(tmp_path, {"archive/old.yaml": "base: !include common/base.yaml\n"})

    with patch.object(devices, "get_devices", return_value=_users()):
        result = await files.rename(
            path="common/base.yaml", new_path="common/main.yaml", skip=["archive/old.yaml"]
        )

    assert result == {
        "path": "common/main.yaml",
        "rewritten": ["kitchen.yaml"],
        "unresolved": ["garage.yaml"],
    }
    old = (tmp_path / "archive/old.yaml").read_text(encoding="utf-8")
    assert old == "base: !include common/base.yaml\n"
    assert "archive/old.yaml" not in {c for c, _ in commits}


async def test_rename_warns_through_unskipped_files_only(
    tmp_path: Path, files: FilesController, setup: tuple[FilesController, object, list]
) -> None:
    _, devices, _ = setup
    _tree(
        tmp_path,
        {
            "kitchen.yaml": "b: !include common/base.yaml\n",
            "garage.yaml": "b: !include bridge/b.yaml\n",
            "shed.yaml": "b: !include bridge/b.yaml\nd: !include common/dyn.yaml\n",
            "bridge/b.yaml": "b: !include ../common/base.yaml\n",
            "common/dyn.yaml": "x: !include ${d}/base.yaml\n",
        },
    )
    users = [
        SimpleNamespace(configuration="kitchen.yaml", dependencies=("common/base.yaml",)),
        SimpleNamespace(
            configuration="garage.yaml", dependencies=("bridge/b.yaml", "common/base.yaml")
        ),
        SimpleNamespace(
            configuration="shed.yaml",
            dependencies=("bridge/b.yaml", "common/base.yaml", "common/dyn.yaml"),
        ),
    ]

    with patch.object(devices, "get_devices", return_value=users):
        result = await files.rename(
            path="common/base.yaml",
            new_path="common/main.yaml",
            skip=["kitchen.yaml", "bridge/b.yaml"],
        )

    # skipped files are no evidence; shed still reaches the old name through common/dyn.yaml
    assert result["rewritten"] == []
    assert result["unresolved"] == ["shed.yaml"]


async def test_rename_reports_an_archived_file_it_could_not_rewrite(
    tmp_path: Path, files: FilesController, setup: tuple[FilesController, object, list]
) -> None:
    _, devices, _ = setup
    _tree(tmp_path, {"archive/old.yaml": "base: !include ${d}/base.yaml\n"})

    with patch.object(devices, "get_devices", return_value=_users()):
        result = await files.rename(path="common/base.yaml", new_path="common/main.yaml")

    assert result["unresolved"] == ["archive/old.yaml", "garage.yaml"]


@pytest.mark.parametrize("skip", ["archive/old.yaml", ["a.yaml", 1], {"a.yaml": 1}])
async def test_rename_refuses_a_malformed_skip(
    tmp_path: Path,
    files: FilesController,
    setup: tuple[FilesController, object, list],
    skip: Any,
) -> None:
    _, _, commits = setup

    with pytest.raises(CommandError) as exc_info:
        await files.rename(path="common/base.yaml", new_path="common/main.yaml", skip=skip)

    assert exc_info.value.code is ErrorCode.INVALID_ARGS
    assert (tmp_path / "common/base.yaml").exists()
    assert commits == []


@pytest.mark.parametrize("operation", [archive.unarchive_single, archive.delete_archived_single])
async def test_archive_operations_wait_for_a_rename_rewriting_the_archived_file(
    tmp_path: Path,
    files: FilesController,
    setup: tuple[FilesController, Any, list],
    operation: Any,
) -> None:
    _, devices, _ = setup
    _tree(tmp_path, {"archive/old.yaml": "base: !include common/base.yaml\n"})
    replanning, resume = threading.Event(), threading.Event()
    calls = 0

    def _pause_under_the_locks(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            replanning.set()
            resume.wait(5)
        return plan_rename(*args, **kwargs)

    with patch(f"{controller_module.__name__}.plan_rename", _pause_under_the_locks):
        rename = asyncio.create_task(
            files.rename(path="common/base.yaml", new_path="common/main.yaml")
        )
        await asyncio.to_thread(replanning.wait, 5)
        other = asyncio.create_task(operation(devices, "old.yaml"))
        done, _ = await asyncio.wait({other}, timeout=0.2)
        resume.set()
        await rename
        await other

    assert not done
    assert not (tmp_path / "archive/old.yaml").exists()
    if operation is archive.unarchive_single:
        restored = (tmp_path / "old.yaml").read_text(encoding="utf-8")
        assert restored == "base: !include common/main.yaml\n"


@pytest.mark.parametrize("rel", ["garage.yaml", "archive/old.yaml"])
async def test_rename_reports_a_multiline_flow_include(
    tmp_path: Path,
    files: FilesController,
    setup: tuple[FilesController, object, list],
    rel: str,
) -> None:
    _, devices, _ = setup
    _tree(
        tmp_path,
        {"garage.yaml": "x: 1\n", rel: 'b: !include {\n  file: "common/${name}.yaml"\n}\n'},
    )

    with patch.object(devices, "get_devices", return_value=_users()):
        result = await files.rename(path="common/base.yaml", new_path="common/main.yaml")

    assert result["rewritten"] == ["kitchen.yaml"]
    assert rel in result["unresolved"]


@pytest.mark.parametrize(
    "text",
    [
        "b: !include { ?\n",
        "b: !include {file:\n",
        "b: !include\n  file: common/base.yaml\n  vars: [\n",
    ],
)
def test_incomplete_yaml_is_unreadable(tmp_path: Path, text: str) -> None:
    _tree(tmp_path, {"common/wrapper.yaml": text, "kitchen.yaml": "b: !include common/base.yaml\n"})

    assert find_referrers(tmp_path, "common/base.yaml") == (["kitchen.yaml"], ["kitchen.yaml"])
    plan = plan_rename(tmp_path, "common/base.yaml", "common/main.yaml")
    assert set(plan.texts) == {"kitchen.yaml"}
    assert "common/wrapper.yaml" in plan.remaining


# The loader decodes both to ``base.yaml``, but the source isn't that text verbatim.
_UNREWRITABLE = ['b: !include "\\u0062ase.yaml"\n', "b: !include >-\n  base.yaml\n"]


@pytest.mark.parametrize("text", _UNREWRITABLE)
def test_unrewritable_includes_are_referrers_but_not_rewritable(tmp_path: Path, text: str) -> None:
    _tree(tmp_path, {"common/base.yaml": "a: base\n", "common/wrapper.yaml": text})
    assert yaml_util.load_yaml(tmp_path / "common/wrapper.yaml")["b"].load() == {"a": "base"}

    referrers = find_referrers(tmp_path, "common/base.yaml")
    plan = plan_rename(tmp_path, "common/base.yaml", "common/main.yaml")

    assert referrers == (["common/wrapper.yaml"], [])
    assert plan.texts == {}
    assert plan.stale == {"common/wrapper.yaml"}


@pytest.mark.parametrize("text", _UNREWRITABLE)
async def test_rename_reports_a_package_it_could_not_rewrite(
    tmp_path: Path,
    files: FilesController,
    setup: tuple[FilesController, object, list],
    text: str,
) -> None:
    _, devices, _ = setup
    _tree(tmp_path, {"common/wrapper.yaml": text})

    # no device uses the file, so only the direct scan can flag the package
    with patch.object(devices, "get_devices", return_value=[]):
        usage = await files.usage(path="common/base.yaml")
        result = await files.rename(path="common/base.yaml", new_path="common/main.yaml")

    assert usage["referenced_by"] == ["common/wrapper.yaml", "kitchen.yaml"]
    assert usage["rewritable"] == ["kitchen.yaml"]
    assert result["rewritten"] == ["kitchen.yaml"]
    assert result["unresolved"] == ["common/wrapper.yaml"]


async def test_usage_and_rename_skip_an_unrelated_incomplete_file(
    tmp_path: Path, files: FilesController, setup: tuple[FilesController, object, list]
) -> None:
    _, devices, _ = setup
    _tree(tmp_path, {"common/broken.yaml": "b: !include { ?\n"})

    with patch.object(devices, "get_devices", return_value=_users()):
        usage = await files.usage(path="common/wifi.yaml")
        result = await files.rename(path="common/wifi.yaml", new_path="common/net.yaml")

    assert usage["referenced_by"] == ["common/base.yaml"]
    assert result["rewritten"] == ["common/base.yaml"]
