"""Tests for ``validate_rewritten_yaml_or_raise``'s tolerate / strict paths."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from esphome_device_builder.controllers.devices import mutations_yaml
from esphome_device_builder.controllers.editor import ValidatorUnavailableError


@pytest.mark.parametrize(
    "exc",
    [TimeoutError(), ValidatorUnavailableError("subprocess died"), BrokenPipeError()],
)
async def test_strict_path_propagates_validator_failure_and_cleans_up(exc: Exception) -> None:
    """Default (strict) callers re-raise a validator timeout / subprocess error and roll back."""
    editor = MagicMock()
    editor.validate_yaml = AsyncMock(side_effect=exc)
    cleanup = Mock()

    with pytest.raises(type(exc)):
        await mutations_yaml.validate_rewritten_yaml_or_raise(
            editor,
            "kitchen.yaml",
            "esphome:\n",
            action="rename",
            on_error_cleanup=cleanup,
        )

    cleanup.assert_called_once()


@pytest.mark.parametrize(
    "exc",
    [TimeoutError(), ValidatorUnavailableError("subprocess died"), BrokenPipeError()],
)
async def test_tolerate_path_keeps_file_on_validator_failure(exc: Exception) -> None:
    """``tolerate_unavailable`` swallows the failure: no raise, no cleanup."""
    editor = MagicMock()
    editor.validate_yaml = AsyncMock(side_effect=exc)
    cleanup = Mock()

    await mutations_yaml.validate_rewritten_yaml_or_raise(
        editor,
        "kitchen.yaml",
        "esphome:\n",
        action="import",
        on_error_cleanup=cleanup,
        tolerate_unavailable=True,
    )

    cleanup.assert_not_called()


async def test_tolerate_path_still_propagates_generic_runtime_error() -> None:
    """A generic RuntimeError isn't subprocess-unavailability; it surfaces even when tolerating."""
    editor = MagicMock()
    editor.validate_yaml = AsyncMock(side_effect=RuntimeError("unexpected bug"))
    cleanup = Mock()

    with pytest.raises(RuntimeError, match="unexpected bug"):
        await mutations_yaml.validate_rewritten_yaml_or_raise(
            editor,
            "kitchen.yaml",
            "esphome:\n",
            action="import",
            on_error_cleanup=cleanup,
            tolerate_unavailable=True,
        )

    cleanup.assert_called_once()
