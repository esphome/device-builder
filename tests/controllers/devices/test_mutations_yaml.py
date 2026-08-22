"""Tests for ``validate_rewritten_yaml_or_raise``'s tolerate / strict paths."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from esphome_device_builder.controllers.devices import mutations_yaml
from esphome_device_builder.controllers.editor import ValidatorUnavailableError
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode


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


async def test_cleanup_failure_preserves_the_validation_error() -> None:
    """A raising rollback callback doesn't replace the original diagnostic."""
    editor = MagicMock()
    editor.validate_yaml = AsyncMock(
        return_value={
            "yaml_errors": [],
            "validation_errors": [{"message": "[esphome] invalid key"}],
        }
    )
    cleanup = Mock(side_effect=OSError("permission denied"))

    with pytest.raises(CommandError) as excinfo:
        await mutations_yaml.validate_rewritten_yaml_or_raise(
            editor,
            "kitchen.yaml",
            "esphome:\n",
            action="rename",
            on_error_cleanup=cleanup,
        )

    assert "invalid key" in excinfo.value.message
    cleanup.assert_called_once()


def test_packages_block_span_bounds() -> None:
    """No ``packages:`` block, and a block running to EOF, both yield ``None``."""
    assert mutations_yaml.packages_block_span("esphome:\n  name: x\n") is None
    # EOF-unbounded span must fail closed, not classify every trailing error.
    assert mutations_yaml.packages_block_span("esphome:\n  name: x\npackages:\n  a: b\n") is None
    assert mutations_yaml.packages_block_span("packages:\n  a: b\nesphome:\n  name: x\n") == (0, 2)


def _dup_key_in(secrets: Path) -> str:
    return (
        'Duplicate key "wifi_password"\n'
        f'  in "{secrets}", line 7, column 1\n'
        "NOTE: Previous declaration here:\n"
        f'  in "{secrets}", line 5, column 1'
    )


def test_secrets_file_problem_folds_a_duplicate_key_in_the_config_secrets(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets.yaml"
    assert mutations_yaml._secrets_file_problem([_dup_key_in(secrets)], secrets) == (
        'has a duplicate key "wifi_password" (lines 5 and 7)'
    )


def test_secrets_file_problem_matches_a_relative_config_dir() -> None:
    """``--dev configs`` leaves config_dir relative; the mark carries the same string."""
    secrets = Path("configs") / "secrets.yaml"
    assert mutations_yaml._secrets_file_problem([_dup_key_in(secrets)], secrets) is not None


def test_secrets_file_problem_does_not_fold_marks_that_straddle_files(tmp_path: Path) -> None:
    """A duplicate-key error with one mark elsewhere keeps the honest trimmed form."""
    secrets = tmp_path / "secrets.yaml"
    error = (
        'Duplicate key "wifi_password"\n'
        f'  in "{secrets}", line 7, column 1\n'
        "NOTE: Previous declaration here:\n"
        f'  in "{tmp_path / "other.yaml"}", line 5, column 1'
    )
    problem = mutations_yaml._secrets_file_problem([error], secrets)
    assert problem is not None
    assert problem.startswith("doesn't parse: Duplicate key")
    assert "in other.yaml, line 5" in problem


def test_secrets_file_problem_summarises_several_errors(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets.yaml"
    other = f'mapping values are not allowed here\n  in "{secrets}", line 3, column 5'
    problem = mutations_yaml._secrets_file_problem([_dup_key_in(secrets), other], secrets)
    assert problem is not None
    assert problem.startswith('doesn\'t parse: Duplicate key "wifi_password" in secrets.yaml')
    assert "mapping values are not allowed here in secrets.yaml, line 3, column 5" in problem


@pytest.mark.parametrize(
    "errors",
    [
        [],
        ["[esphome] generator regression"],
        ['mapping values are not allowed here\n  in "/config/kitchen.yaml", line 3, column 5'],
        ["Secret 'wifi_ssid' not defined\n  in \"/config/kitchen.yaml\", line 9, column 11"],
        # A co-occurring generator error keeps the generic (report it) path.
        [_dup_key_in(Path("/config/secrets.yaml")), "[esphome] generator regression"],
        # A package-cache secrets.yaml is not the file the Secrets page edits.
        [_dup_key_in(Path("/config/.esphome/packages/abc/secrets.yaml"))],
    ],
)
def test_secrets_file_problem_is_none_unless_every_error_sits_in_the_config_secrets(
    errors: list[str],
) -> None:
    assert mutations_yaml._secrets_file_problem(errors, Path("/config/secrets.yaml")) is None


@pytest.mark.parametrize(
    ("on_failure", "logged"),
    [(ErrorCode.INTERNAL_ERROR, True), (ErrorCode.INVALID_ARGS, False)],
)
async def test_secrets_reclassification_logs_only_a_would_be_generator_bug(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, on_failure: ErrorCode, logged: bool
) -> None:
    secrets = tmp_path / "secrets.yaml"
    editor = MagicMock()
    editor.validate_yaml = AsyncMock(
        return_value={"yaml_errors": [{"message": _dup_key_in(secrets)}], "validation_errors": []}
    )
    with caplog.at_level(logging.INFO), pytest.raises(CommandError) as excinfo:
        await mutations_yaml.validate_rewritten_yaml_or_raise(
            editor,
            "kitchen.yaml",
            "esphome:\n",
            action="create",
            on_failure=on_failure,
            secrets_path=secrets,
        )
    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert ("not the generator" in caplog.text) is logged
