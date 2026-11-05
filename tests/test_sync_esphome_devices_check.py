"""Tests for the ``--check``, ``--repo`` and repeatable ``--device`` sync CLI."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from script import sync_esphome_devices as sync  # type: ignore[import-not-found]
from script._sync_check import (  # type: ignore[import-not-found]
    NOT_A_DEVICE_PAGE,
    check_results,
    print_check_results,
)

_PAGE = """\
---
title: {title}
date-published: 2025-01-01
type: plug
standard: eu
---

![Plug](plug.jpg "Plug")

```yaml {fence}
```
"""

_CONFIG = """\
esphome:
  name: plug
esp8266:
  board: esp01_1m
switch:
  - platform: gpio
    name: Relay
    pin: GPIO12
"""


def _device(
    repo: Path,
    name: str,
    *,
    title: str = "Plug",
    image: bool = True,
    fence: str = "file=config.yaml",
) -> None:
    folder = repo / "src" / "docs" / "devices" / name
    folder.mkdir(parents=True)
    (folder / "index.md").write_text(_PAGE.format(title=title, fence=fence))
    (folder / "config.yaml").write_text(_CONFIG)
    if image:
        (folder / "plug.jpg").write_bytes(b"x")


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    with (
        patch.object(sys, "argv", ["sync_esphome_devices.py", *argv]),
        patch.object(sync, "apply_validation_gate", return_value={}) as gate,
        patch.object(sync, "_emit_manifest") as emit,
        patch.object(sync, "_prune_removed") as prune,
        patch.object(sync, "_ensure_devices_repo") as clone,
    ):
        code = sync.main()
    emit.assert_not_called()
    prune.assert_not_called()
    clone.assert_not_called()
    assert gate.called
    return code, capsys.readouterr().out


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        pytest.param({}, None, id="imports"),
        pytest.param({"image": False}, "no local images", id="no-image"),
        pytest.param({"fence": 'file="config.yaml"'}, None, id="quoted-file-ref"),
    ],
)
def test_check_reports_each_requested_device(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kwargs: dict, reason: str | None
) -> None:
    _device(tmp_path, "Plug-A", **kwargs)
    code, out = _run(["--repo", str(tmp_path), "--check", "--json", "--device", "Plug-A"], capsys)
    rows = json.loads(out)
    assert rows == [{"device": "Plug-A", "imported": reason is None, "reason": reason}]
    assert code == (0 if reason is None else 1)


def test_check_takes_repeated_devices_and_flags_missing_ones(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _device(tmp_path, "Plug-A")
    _device(tmp_path, "Plug-B", title="")
    _device(tmp_path, "Plug-C")
    code, out = _run(
        [
            "--repo",
            str(tmp_path),
            "--check",
            "--device",
            "Plug-B",
            "--device",
            "Plug-A",
            "--device",
            "Gone",
        ],
        capsys,
    )
    assert code == 1
    assert out.splitlines() == [
        "skip  Plug-B: no frontmatter title",
        "ok    Plug-A",
        f"skip  Gone: {NOT_A_DEVICE_PAGE}",
    ]


def test_repo_without_devices_tree_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with patch.object(sys, "argv", ["sync_esphome_devices.py", "--repo", str(tmp_path), "--check"]):
        assert sync.main() == 2


def test_check_results_without_requested_lists_every_seen_folder() -> None:
    class Skip:
        folder_name = "B"
        reason = "no local images"

    assert check_results(None, {"A"}, [Skip()]) == [
        {"device": "A", "imported": True, "reason": None},
        {"device": "B", "imported": False, "reason": "no local images"},
    ]


def test_print_check_results_text(capsys: pytest.CaptureFixture[str]) -> None:
    print_check_results([{"device": "A", "imported": True, "reason": None}], as_json=False)
    assert capsys.readouterr().out == "ok    A\n"
