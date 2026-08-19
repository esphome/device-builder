"""Real ``esphome vscode`` pin: a late ``external_components`` override reaches the validator."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from esphome_device_builder.controllers.editor import EditorController

# A from-scratch ``captive_portal`` whose schema accepts a key the
# built-in one rejects; the built-in reports it as an invalid option.
_OVERRIDE_INIT = """
import esphome.config_validation as cv

CONFIG_SCHEMA = cv.Schema({cv.Optional("enable_exfat"): cv.boolean})


async def to_code(config):
    return None
"""

_BASE_YAML = """esphome:
  name: testtest
esp32:
  board: esp32dev
  framework:
    type: esp-idf
logger:
wifi:
  ssid: a
  password: aaaaaaaa
captive_portal:
  enable_exfat: true
"""


def _override_yaml(components_dir: Path) -> str:
    return _BASE_YAML.replace(
        "esp32:\n",
        "external_components:\n"
        "  - source:\n"
        "      type: local\n"
        f"      path: {components_dir}\n"
        "    components: [captive_portal]\n"
        "esp32:\n",
        1,
    )


def _messages(result: dict) -> list[str]:
    return [err["message"] for err in result["validation_errors"]]


@pytest.mark.timeout(300)
async def test_override_added_after_first_validate_takes_effect(tmp_path: Path) -> None:
    """The override validates clean even though the built-in was loaded first."""
    components_dir = tmp_path / "components"
    (components_dir / "captive_portal").mkdir(parents=True)
    (components_dir / "captive_portal" / "__init__.py").write_text(_OVERRIDE_INIT)
    db = MagicMock()
    db.settings.config_dir = tmp_path
    controller = EditorController(db)
    await controller.start()
    try:
        first = await controller.validate_yaml(configuration="testtest.yaml", content=_BASE_YAML)
        assert any("enable_exfat" in message for message in _messages(first))

        second = await controller.validate_yaml(
            configuration="testtest.yaml", content=_override_yaml(components_dir)
        )
        assert not any("enable_exfat" in message for message in _messages(second)), second

        # Dropping the override must bring the built-in schema back too.
        third = await controller.validate_yaml(configuration="testtest.yaml", content=_BASE_YAML)
        assert any("enable_exfat" in message for message in _messages(third))
    finally:
        await controller.stop()
