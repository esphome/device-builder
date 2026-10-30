"""Tests for the include matching and rewriting behind ``files/rename`` and ``files/usage``."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from esphome import yaml_util

from esphome_device_builder.controllers.files import rename as rename_module
from esphome_device_builder.controllers.files.rename import (
    find_referrers,
    plan_rename,
    rewrite_includes,
)

from .conftest import UNREWRITABLE_INCLUDES, write_tree


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


def test_plan_rename_covers_devices_and_packages(tmp_path: Path) -> None:
    write_tree(
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


def test_plan_rename_matches_an_uppercase_extension(tmp_path: Path) -> None:
    write_tree(tmp_path, {"kitchen.yaml": "base: !include common/Base.YAML\n"})

    plan = plan_rename(tmp_path, "common/Base.YAML", "common/Main.YAML")

    assert plan.texts == {"kitchen.yaml": "base: !include common/Main.YAML\n"}
    assert plan.remaining == set()


def test_plan_rename_checks_secrets_and_block_form_substitutions(tmp_path: Path) -> None:
    write_tree(
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


def test_plan_rename_flags_a_file_it_cannot_read(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    write_tree(tmp_path, {"kitchen.yaml": "base: !include common/base.yaml\n"})
    (tmp_path / "binary.yaml").write_bytes(b"\xff\xfe!include common/base.yaml\n")

    plan = plan_rename(tmp_path, "common/base.yaml", "common/main.yaml")

    assert set(plan.texts) == {"kitchen.yaml"}
    assert plan.remaining == {"binary.yaml"}
    assert "binary.yaml" in caplog.text


def test_plan_rename_keeps_crlf_line_endings(tmp_path: Path) -> None:
    (tmp_path / "kitchen.yaml").write_bytes(b"packages:\r\n  base: !include common/base.yaml\r\n")

    plan = plan_rename(tmp_path, "common/base.yaml", "common/main.yaml")

    assert plan.texts["kitchen.yaml"] == "packages:\r\n  base: !include common/main.yaml\r\n"
    assert plan.originals["kitchen.yaml"].count("\r\n") == 2


def test_plan_rename_rewrites_archived_devices_from_the_config_dir(tmp_path: Path) -> None:
    write_tree(tmp_path, {"archive/old.yaml": "packages:\n  base: !include common/base.yaml\n"})

    plan = plan_rename(tmp_path, "common/base.yaml", "common/main.yaml")

    assert plan.texts == {"archive/old.yaml": "packages:\n  base: !include common/main.yaml\n"}


def test_plan_rename_leaves_skipped_files_alone(tmp_path: Path) -> None:
    write_tree(
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
    write_tree(tmp_path, {"kitchen.yaml": text})

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
    write_tree(tmp_path, {"kitchen.yaml": text})

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
    write_tree(
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
    write_tree(tmp_path, {"common/wrapper.yaml": text})

    assert rewrite_includes(
        text, folder="common", old="common/base.yaml", new="common/main.yaml"
    ) == (expected)
    assert find_referrers(tmp_path, "common/base.yaml").rewritable == ["common/wrapper.yaml"]


def test_nested_block_includes_are_found_independently(tmp_path: Path) -> None:
    write_tree(
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
    write_tree(tmp_path, _REFERRER_TREE)

    assert find_referrers(tmp_path, "common/base.yaml").referenced_by == [
        "archive/old.yaml",
        "bridge/plug.yaml",
        "common/extra.yaml",
        "kitchen.yaml",
    ]


@pytest.mark.parametrize("path", ["common/base.yaml", "common/other.yaml", "bridge/plug.yaml"])
def test_find_referrers_matches_what_a_rename_rewrites(tmp_path: Path, path: str) -> None:
    write_tree(tmp_path, _REFERRER_TREE)

    plan = plan_rename(tmp_path, path, path.replace(".yaml", "-new.yaml"))

    referrers = find_referrers(tmp_path, path)

    assert referrers.rewritable == referrers.referenced_by == sorted(plan.texts)


@pytest.mark.parametrize(
    "text",
    [
        "b: !include { ?\n",
        "b: !include {file:\n",
        "b: !include\n  file: common/base.yaml\n  vars: [\n",
    ],
)
def test_incomplete_yaml_is_unreadable(tmp_path: Path, text: str) -> None:
    write_tree(
        tmp_path, {"common/wrapper.yaml": text, "kitchen.yaml": "b: !include common/base.yaml\n"}
    )

    assert find_referrers(tmp_path, "common/base.yaml") == (["kitchen.yaml"], ["kitchen.yaml"])
    plan = plan_rename(tmp_path, "common/base.yaml", "common/main.yaml")
    assert set(plan.texts) == {"kitchen.yaml"}
    assert "common/wrapper.yaml" in plan.remaining


@pytest.mark.parametrize("text", UNREWRITABLE_INCLUDES)
def test_unrewritable_includes_are_referrers_but_not_rewritable(tmp_path: Path, text: str) -> None:
    write_tree(tmp_path, {"common/base.yaml": "a: base\n", "common/wrapper.yaml": text})
    assert yaml_util.load_yaml(tmp_path / "common/wrapper.yaml")["b"].load() == {"a": "base"}

    referrers = find_referrers(tmp_path, "common/base.yaml")
    plan = plan_rename(tmp_path, "common/base.yaml", "common/main.yaml")

    assert referrers == (["common/wrapper.yaml"], [])
    assert plan.texts == {}
    assert plan.stale == {"common/wrapper.yaml"}


@pytest.mark.parametrize(
    "loader",
    [
        pytest.param(yaml.SafeLoader, id="python"),
        pytest.param(
            getattr(yaml, "CSafeLoader", None),
            id="libyaml",
            marks=pytest.mark.skipif(not yaml.__with_libyaml__, reason="PyYAML without libyaml"),
        ),
    ],
)
def test_rewrite_positions_match_with_either_scanner(
    monkeypatch: pytest.MonkeyPatch, loader: type
) -> None:
    monkeypatch.setattr(rename_module, "FastestSafeLoader", loader)
    text = '\ufeffname: "Küche ✓"\r\nb: !include {vars: {t: "ä"}, file: common/base.yaml}\r\n'

    rewritten = rewrite_includes(text, folder="", old="common/base.yaml", new="common/main.yaml")

    assert rewritten == text.replace("common/base.yaml", "common/main.yaml")


def test_case_folding_filesystems_match_any_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rename_module, "dependency_key", str.lower)
    write_tree(
        tmp_path,
        {
            "kitchen.yaml": "b: !include common/Base.yaml\n",
            "garage.yaml": "note: Kept in BASE.YAML\n",
        },
    )

    plan = plan_rename(tmp_path, "common/base.yaml", "common/main.yaml")

    assert plan.texts == {"kitchen.yaml": "b: !include common/main.yaml\n"}
    assert plan.remaining == {"garage.yaml"}
    assert find_referrers(tmp_path, "common/base.yaml").rewritable == ["kitchen.yaml"]
