"""Coverage for the ``firmware/download_token`` WS command.

It mints the single-use capability token that authorizes one HTTP artifact
download (``GET /api/firmware/download``). Like every config-taking command it
validates the configuration boundary before minting, and the returned token
resolves back to the requested ``(configuration, file)``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode
from tests.controllers.firmware.conftest import FirmwareControllerFactory


async def test_download_token_mints_token_bound_to_artifact(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    controller = firmware_controller_factory()

    result = await controller.download_token(configuration="kitchen.yaml", file="firmware.elf")

    token = result["token"]
    assert isinstance(token, str) and token
    # The token carries the artifact it was minted for, and is single-use.
    assert controller.download_tokens.consume(token) == ("kitchen.yaml", "firmware.elf")
    assert controller.download_tokens.consume(token) is None


async def test_download_token_rejects_traversal(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """``firmware/download_token`` re-validates the configuration boundary."""
    controller = firmware_controller_factory()

    with pytest.raises(CommandError) as excinfo:
        await controller.download_token(configuration="../etc/passwd", file="firmware.bin")
    assert excinfo.value.code == ErrorCode.INVALID_ARGS
