"""Real-``esphome vscode`` pin for the adopt package-failure classification (#2424)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from esphome.const import __version__ as esphome_version

from esphome_device_builder.controllers.devices.mutations_yaml import (
    _INHERIT_ERROR_MARK,
    _packages_confined_warning,
    packages_block_span,
)
from esphome_device_builder.helpers.device_yaml import generate_adoption_yaml
from esphome_device_builder.helpers.version_compat import version_at_least


def _validate_via_vscode(tmp_path: Path, content: str) -> dict:
    """Run one real ``esphome vscode --ace`` validation round-trip on *content*."""
    with subprocess.Popen(  # noqa: S603 — args are fully test-controlled
        [sys.executable, "-m", "esphome", "vscode", str(tmp_path), "--ace"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        cwd=tmp_path,
        # Never block on an interactive git credential prompt; the
        # dead-repo probe must fail, not ask.
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""},
    ) as proc:
        try:
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(json.dumps({"type": "validate", "file": "adopt.yaml"}) + "\n")
            proc.stdin.flush()
            deadline = time.monotonic() + 120
            while True:
                assert time.monotonic() < deadline, "validator round-trip exceeded 120s"
                line = proc.stdout.readline()
                assert line, "esphome vscode subprocess closed stdout"
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") == "read_file":
                    proc.stdin.write(
                        json.dumps({"type": "file_response", "content": content}) + "\n"
                    )
                    proc.stdin.flush()
                elif msg.get("type") == "result":
                    return {
                        "yaml_errors": msg.get("yaml_errors", []),
                        "validation_errors": msg.get("validation_errors", []),
                    }
        finally:
            proc.kill()


def test_unresolvable_package_errors_root_inside_the_packages_span(tmp_path: Path) -> None:
    """Esphome fails early on the package and roots every error inside its block."""
    content = generate_adoption_yaml(
        "gl-s10-test",
        "GL S10",
        "gl-inet.gl-s10",
        "github://esphome/non-existent-repo-2424/gl-s10.yaml@main",
        network_provided=True,
        api_encryption=False,
    )
    result = _validate_via_vscode(tmp_path, content)

    assert result["validation_errors"], "expected the package failure to surface"
    span = packages_block_span(content)
    warning = _packages_confined_warning(
        result, span, tmp_path / ".esphome" / "packages", "adopt.yaml", "import"
    )
    assert warning is not None
    assert "Fix the packages entry" in warning.text


_BARE_OTA_PACKAGE_YAML = """\
esphome:
  name: ${name}
esp8266:
  board: esp_wroom_02
api:
  reboot_timeout: 0s
ota:
  - platform: esphome
    encryption:
wifi:
  ssid: testnet
  password: longenough1
"""


def test_bare_ota_encryption_error_carries_the_inherit_mark(tmp_path: Path) -> None:
    """Esphome's error for a bare ``ota: encryption:`` without an api key names the inherit."""
    if not version_at_least(esphome_version, "2026.10.0"):
        pytest.skip("installed esphome predates OTA key inheritance")
    content = (
        "substitutions:\n  name: kitchen\n\npackages:\n  v: !include package.yaml\n\n"
        "esphome:\n  name: ${name}\n"
    )
    (tmp_path / "package.yaml").write_text(_BARE_OTA_PACKAGE_YAML, encoding="utf-8")
    result = _validate_via_vscode(tmp_path, content)

    messages = [str(e.get("message", "")) for e in result["validation_errors"]]
    assert messages and all(_INHERIT_ERROR_MARK in m for m in messages), messages
