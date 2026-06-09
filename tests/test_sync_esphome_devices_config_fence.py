"""
Tests for ``yaml`` config-fence resolution in ``script/sync_esphome_devices.py``.

Pins the ``file=config.yaml`` sibling-reference path: device pages whose
config lives in a sibling file (the rendered site inlines it) must parse
instead of being dropped as if they had no config.
"""

from __future__ import annotations

from pathlib import Path

from script.sync_esphome_devices import _first_config_yaml  # type: ignore[import-not-found]

_INLINE_BODY = """\
## Basic Config

```yaml
esphome:
  name: inline-device
bk72xx:
  board: cb2s
```
"""

_FILE_BODY = """\
## Basic Config

```yaml file=config.yaml
```
"""


def test_reads_inline_fence(tmp_path: Path) -> None:
    parsed = _first_config_yaml(_INLINE_BODY, tmp_path)
    assert parsed is not None
    assert parsed[0]["bk72xx"]["board"] == "cb2s"


def test_follows_file_reference(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text("esphome:\n  name: x\nbk72xx:\n  board: cb2s\n")
    parsed = _first_config_yaml(_FILE_BODY, tmp_path)
    assert parsed is not None
    assert parsed[0]["bk72xx"]["board"] == "cb2s"
    # The raw text is the sibling file's content (drives the source hash).
    assert "board: cb2s" in parsed[1]


def test_missing_file_falls_through(tmp_path: Path) -> None:
    assert _first_config_yaml(_FILE_BODY, tmp_path) is None


def test_skips_url_reference(tmp_path: Path) -> None:
    body = "```yaml url=https://example.com/c.yaml\n```\n"
    assert _first_config_yaml(body, tmp_path) is None


def test_rejects_path_traversal(tmp_path: Path) -> None:
    (tmp_path.parent / "secret.yaml").write_text("esphome:\n  name: leak\n")
    body = "```yaml file=../secret.yaml\n```\n"
    assert _first_config_yaml(body, tmp_path) is None


def test_rejects_subdirectory_reference(tmp_path: Path) -> None:
    sub = tmp_path / "configs"
    sub.mkdir()
    (sub / "device.yaml").write_text("esphome:\n  name: x\nbk72xx:\n  board: cb2s\n")
    body = "```yaml file=configs/device.yaml\n```\n"
    assert _first_config_yaml(body, tmp_path) is None


def test_strips_dot_slash_prefix(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text("esphome:\n  name: x\nbk72xx:\n  board: cb2s\n")
    body = "```yaml file=./config.yaml\n```\n"
    parsed = _first_config_yaml(body, tmp_path)
    assert parsed is not None
    assert parsed[0]["bk72xx"]["board"] == "cb2s"
