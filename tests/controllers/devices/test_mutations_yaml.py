"""Tests for ``validate_rewritten_yaml_or_raise``'s tolerate / strict paths."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.controllers.devices import mutations_yaml
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode

from .conftest import VALIDATOR_OUTAGES


@pytest.mark.parametrize("exc", VALIDATOR_OUTAGES)
async def test_strict_path_propagates_validator_failure(exc: Exception) -> None:
    """Default (strict) callers re-raise a validator outage."""
    editor = MagicMock()
    editor.validate_yaml = AsyncMock(side_effect=exc)

    with pytest.raises(type(exc)):
        await mutations_yaml.validate_rewritten_yaml_or_raise(
            editor,
            "kitchen.yaml",
            "esphome:\n",
            action="rename",
        )


@pytest.mark.parametrize("exc", VALIDATOR_OUTAGES)
async def test_tolerate_path_keeps_file_on_validator_failure(exc: Exception) -> None:
    """``tolerate_unavailable`` hands the outage back as the verdict instead of raising."""
    editor = MagicMock()
    editor.validate_yaml = AsyncMock(side_effect=exc)

    verdict = await mutations_yaml.validate_rewritten_yaml_or_raise(
        editor,
        "kitchen.yaml",
        "esphome:\n",
        action="import",
        tolerate_unavailable=True,
    )

    assert verdict == mutations_yaml.ValidationVerdict(unavailable=True)


async def test_tolerate_path_still_propagates_generic_runtime_error() -> None:
    """A generic RuntimeError isn't subprocess-unavailability; it surfaces even when tolerating."""
    editor = MagicMock()
    editor.validate_yaml = AsyncMock(side_effect=RuntimeError("unexpected bug"))

    with pytest.raises(RuntimeError, match="unexpected bug"):
        await mutations_yaml.validate_rewritten_yaml_or_raise(
            editor,
            "kitchen.yaml",
            "esphome:\n",
            action="import",
            tolerate_unavailable=True,
        )


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


_INHERIT_ERROR = f"no 'api' {mutations_yaml._INHERIT_ERROR_MARK}; set one of them"


@pytest.mark.parametrize(
    ("messages", "only_missing_api_key"),
    [
        pytest.param([f"complaint {n}" for n in range(3)] + [_INHERIT_ERROR], False, id="mixed"),
        pytest.param([_INHERIT_ERROR] * 4, True, id="all"),
    ],
)
def test_packages_confined_warning_classifies_every_message(
    tmp_path: Path, messages: list[str], only_missing_api_key: bool
) -> None:
    """The inherit verdict reads every confined message, not just the three the text shows."""
    content = "packages:\n  v: github://x/y.yaml@main\n\nesphome:\n  name: kitchen\n"
    span = mutations_yaml.packages_block_span(content)
    assert span is not None
    entries = [
        {"message": m, "range": {"document": "<file>", "start_line": span[0]}} for m in messages
    ]

    warning = mutations_yaml._packages_confined_warning(
        {"yaml_errors": [], "validation_errors": entries}, span, tmp_path, "kitchen.yaml", "import"
    )

    assert warning is not None
    assert warning.only_missing_api_key is only_missing_api_key
    assert "(+1 more)" in warning.text


_STUB_WITH_SECRETS = (
    "esphome:\n  name: kitchen\n\nwifi:\n  ssid: !secret wifi_ssid\n"
    "  password: !secret wifi_password\n"
)
_WPA_TOO_SHORT = "WPA password must be at least 8 characters long."


def _pinned_to(content: str, token: str, message: str, document: str = "<file>") -> dict:
    """Return a validator entry whose range covers *token* in *content*."""
    line = next(i for i, text in enumerate(content.splitlines()) if token in text)
    col = content.splitlines()[line].index(token)
    return {
        "range": {
            "document": document,
            "start_line": line,
            "start_col": col,
            "end_line": line,
            "end_col": col + len(token),
        },
        "message": message,
    }


def test_secret_value_problem_names_the_secret_the_error_sits_on() -> None:
    result = {
        "yaml_errors": [],
        "validation_errors": [
            _pinned_to(_STUB_WITH_SECRETS, "!secret wifi_password", _WPA_TOO_SHORT)
        ],
    }
    assert mutations_yaml._secret_value_problem(result, _STUB_WITH_SECRETS) == (
        "has an invalid wifi_password: WPA password must be at least 8 characters long"
    )


def test_secret_value_problem_unquotes_the_secret_name() -> None:
    content = "wifi:\n  password: !secret 'wifi_password'\n"
    result = {
        "yaml_errors": [],
        "validation_errors": [_pinned_to(content, "!secret 'wifi_password'", _WPA_TOO_SHORT)],
    }
    assert mutations_yaml._secret_value_problem(result, content) == (
        "has an invalid wifi_password: WPA password must be at least 8 characters long"
    )


def test_secret_value_problem_summarises_several_secrets() -> None:
    result = {
        "yaml_errors": [],
        "validation_errors": [
            _pinned_to(
                _STUB_WITH_SECRETS, "!secret wifi_ssid", "SSID can't be longer than 32 characters."
            ),
            _pinned_to(_STUB_WITH_SECRETS, "!secret wifi_password", _WPA_TOO_SHORT),
        ],
    }
    assert mutations_yaml._secret_value_problem(result, _STUB_WITH_SECRETS) == (
        "has invalid values: wifi_ssid: SSID can't be longer than 32 characters; "
        "wifi_password: WPA password must be at least 8 characters long"
    )


@pytest.mark.parametrize(
    "result",
    [
        {"yaml_errors": [], "validation_errors": []},
        # A parse error anywhere keeps the generic path.
        {
            "yaml_errors": [{"message": "mapping values are not allowed here"}],
            "validation_errors": [
                _pinned_to(_STUB_WITH_SECRETS, "!secret wifi_password", _WPA_TOO_SHORT)
            ],
        },
        # The range covers a plain value, not a secret reference.
        {
            "yaml_errors": [],
            "validation_errors": [_pinned_to(_STUB_WITH_SECRETS, "kitchen", "bad name")],
        },
        # A co-occurring generator error keeps the generic (report it) path.
        {
            "yaml_errors": [],
            "validation_errors": [
                _pinned_to(_STUB_WITH_SECRETS, "!secret wifi_password", _WPA_TOO_SHORT),
                {"range": None, "message": "[esphome] generator regression"},
            ],
        },
        # A secret pinned in another document isn't this file's value.
        {
            "yaml_errors": [],
            "validation_errors": [
                _pinned_to(
                    _STUB_WITH_SECRETS,
                    "!secret wifi_password",
                    _WPA_TOO_SHORT,
                    document="/config/.esphome/packages/abc/base.yaml",
                )
            ],
        },
        # A secret the generator never emits is the generator's bug, not the user's.
        {
            "yaml_errors": [],
            "validation_errors": [
                _pinned_to(
                    "api:\n  encryption:\n    key: !secret api_key\n",
                    "!secret api_key",
                    "Invalid key format",
                )
            ],
        },
        # An empty message has nothing to attribute.
        {
            "yaml_errors": [],
            "validation_errors": [_pinned_to(_STUB_WITH_SECRETS, "!secret wifi_password", "")],
        },
        # Column bounds that aren't ints.
        {
            "yaml_errors": [],
            "validation_errors": [
                {
                    "range": {
                        "document": "<file>",
                        "start_line": 5,
                        "start_col": None,
                        "end_line": 5,
                        "end_col": None,
                    },
                    "message": _WPA_TOO_SHORT,
                }
            ],
        },
        # A multi-line range is a mapping, not a scalar secret.
        {
            "yaml_errors": [],
            "validation_errors": [
                {
                    "range": {
                        "document": "<file>",
                        "start_line": 3,
                        "start_col": 0,
                        "end_line": 5,
                        "end_col": 33,
                    },
                    "message": _WPA_TOO_SHORT,
                }
            ],
        },
        # A range past the end of the content.
        {
            "yaml_errors": [],
            "validation_errors": [
                {
                    "range": {
                        "document": "<file>",
                        "start_line": 40,
                        "start_col": 0,
                        "end_line": 40,
                        "end_col": 5,
                    },
                    "message": _WPA_TOO_SHORT,
                }
            ],
        },
    ],
)
def test_secret_value_problem_is_none_unless_every_error_sits_on_a_secret(result: dict) -> None:
    assert mutations_yaml._secret_value_problem(result, _STUB_WITH_SECRETS) is None


async def test_secret_value_error_refuses_as_invalid_args_even_for_generator_output() -> None:
    """A ``!secret`` value esphome rejects is the user's to fix, whatever ``on_failure`` says."""
    editor = MagicMock()
    editor.validate_yaml = AsyncMock(
        return_value={
            "yaml_errors": [],
            "validation_errors": [
                _pinned_to(_STUB_WITH_SECRETS, "!secret wifi_password", _WPA_TOO_SHORT)
            ],
        }
    )

    with pytest.raises(CommandError) as excinfo:
        await mutations_yaml.validate_rewritten_yaml_or_raise(
            editor,
            "kitchen.yaml",
            _STUB_WITH_SECRETS,
            action="create",
            on_failure=ErrorCode.INTERNAL_ERROR,
        )

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert excinfo.value.message == (
        "Can't create: secrets.yaml has an invalid wifi_password: WPA password must be "
        "at least 8 characters long. Fix it on the Secrets page and try again."
    )


async def test_secret_value_error_in_user_yaml_keeps_the_editor_refusal() -> None:
    """A ``!secret`` the user placed is theirs to fix in the editor, with the caller's tail."""
    editor = MagicMock()
    editor.validate_yaml = AsyncMock(
        return_value={
            "yaml_errors": [],
            "validation_errors": [
                _pinned_to(_STUB_WITH_SECRETS, "!secret wifi_password", _WPA_TOO_SHORT)
            ],
        }
    )

    with pytest.raises(CommandError) as excinfo:
        await mutations_yaml.validate_rewritten_yaml_or_raise(
            editor,
            "kitchen.yaml",
            _STUB_WITH_SECRETS,
            action="rename",
            failure_tail=". Fix the config and retry the rename.",
        )

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert excinfo.value.message == (
        "Can't rename — config doesn't validate: WPA password must be at least 8 "
        "characters long. Fix the config and retry the rename."
    )
