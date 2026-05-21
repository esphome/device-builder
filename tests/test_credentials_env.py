"""Tests for ``ESPHOME_USERNAME`` / ``ESPHOME_PASSWORD`` env-var handling.

Two layers exercise the same precedence rules:

* ``__main__._validate_credentials`` — the CLI-time mismatch guard
  that errors out before ``DashboardSettings`` is even built when
  one half of the credential pair is set without the other.
* ``DashboardSettings.parse_args`` — the actual env-var lookup
  that populates ``settings.username`` / ``settings.password_hash``.

Both layers must:

* Read ``$ESPHOME_USERNAME`` / ``$ESPHOME_PASSWORD`` as fallbacks
  for ``--username`` / ``--password``.
* Let CLI flags win over env vars.
* **Ignore bare ``$USERNAME`` / ``$PASSWORD``** — those collide
  with the OS user on Linux/Windows shells and are the regression
  the rename fixes.
"""

from __future__ import annotations

import argparse
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from esphome_device_builder.__main__ import (
    _validate_credentials,
    _warn_deprecated_credential_flags,
)
from esphome_device_builder.controllers.config import DashboardSettings


def _ns(configuration: str, **kwargs: object) -> SimpleNamespace:
    """Minimal argparse-namespace stub for ``DashboardSettings.parse_args``.

    Same shape as the helper in ``test_trusted_domains.py`` — defaults
    cover every attribute ``parse_args`` reads so the credential test
    only needs to override ``username`` / ``password``.
    """
    defaults: dict[str, object] = {
        "ha_addon": False,
        "configuration": configuration,
        "username": "",
        "password": "",
        "log_level": "info",
        "port": 6052,
        "host": "0.0.0.0",
        "ingress_port": 6053,
        "ingress_host": "",
        "dev": False,
        "trusted_domains": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _validate(env: dict[str, str], *, username: str = "", password: str = "") -> None:
    """Run ``_validate_credentials`` under a clean env populated from ``env``.

    ``clear=True`` wipes any inherited ``USERNAME`` / ``PASSWORD`` /
    ``ESPHOME_*`` vars from the developer's shell so the test sees
    exactly what it sets — otherwise running the suite on Linux
    (where ``USERNAME`` is set by the login shell) would conflate
    inherited env with what each test means to assert on.
    """
    parser = argparse.ArgumentParser()
    args = argparse.Namespace(username=username, password=password)
    with patch.dict(os.environ, env, clear=True):
        _validate_credentials(parser, args)


# ---------------------------------------------------------------------------
# _validate_credentials — accepted combinations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "username", "password"),
    [
        # Nothing set anywhere — unauthenticated startup, no error.
        ({}, "", ""),
        # Both halves on the CLI.
        ({}, "admin", "hunter2"),
        # Both halves in the env.
        ({"ESPHOME_USERNAME": "admin", "ESPHOME_PASSWORD": "hunter2"}, "", ""),
        # Mix-and-match: CLI username + env password.
        ({"ESPHOME_PASSWORD": "hunter2"}, "admin", ""),
        # Mix-and-match: env username + CLI password.
        ({"ESPHOME_USERNAME": "admin"}, "", "hunter2"),
        # Bare ``USERNAME`` / ``PASSWORD`` together — the regression
        # case. The login-shell-set vars must not satisfy either side
        # of the both-or-neither check, so this lands on the "both
        # unset" branch and starts unauthenticated.
        ({"USERNAME": "jesse", "PASSWORD": "shellsecret"}, "", ""),
    ],
)
def test_validate_credentials_accepts(env: dict[str, str], username: str, password: str) -> None:
    """Matched-pair (or both-unset) inputs pass without raising."""
    _validate(env, username=username, password=password)


# ---------------------------------------------------------------------------
# _validate_credentials — rejected combinations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "username", "password"),
    [
        # CLI half only.
        ({}, "admin", ""),
        ({}, "", "hunter2"),
        # Env half only.
        ({"ESPHOME_USERNAME": "admin"}, "", ""),
        ({"ESPHOME_PASSWORD": "hunter2"}, "", ""),
        # Bare ``USERNAME`` paired with a real ``--password`` must
        # still error: the bare var doesn't count as a username, so
        # only one half of the pair is actually set. Without this,
        # the dashboard would silently start with ``username="jesse"``
        # and whatever the operator typed for ``--password``.
        ({"USERNAME": "jesse"}, "", "hunter2"),
        # Symmetric: bare ``PASSWORD`` + real ``--username``.
        ({"PASSWORD": "shellsecret"}, "admin", ""),
    ],
)
def test_validate_credentials_rejects_mismatch(
    env: dict[str, str], username: str, password: str
) -> None:
    """Only one half set → ``parser.error`` → ``SystemExit(2)``."""
    with pytest.raises(SystemExit) as excinfo:
        _validate(env, username=username, password=password)
    assert excinfo.value.code == 2


def test_validate_credentials_error_message_names_new_env_vars(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Error text points operators at the new env-var names.

    Pin the message so a future tweak that resurrects ``$USERNAME``
    / ``$PASSWORD`` in the user-facing copy fails this test —
    keeps docs (the help text, the warning banner, and this error)
    aligned with the actual behaviour.
    """
    with pytest.raises(SystemExit):
        _validate({}, username="admin", password="")
    err = capsys.readouterr().err
    assert "$ESPHOME_USERNAME" in err
    assert "$ESPHOME_PASSWORD" in err
    # Defense in depth: the bare names should not appear in the
    # message either, since pointing operators at them would
    # reintroduce the system-var collision footgun.
    assert "$USERNAME" not in err
    assert "$PASSWORD" not in err


# ---------------------------------------------------------------------------
# _warn_deprecated_credential_flags — log on --username / --password
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("username", "password"),
    [
        ("admin", "hunter2"),
        ("admin", ""),
        ("", "hunter2"),
    ],
)
def test_warn_deprecated_credential_flags_logs_when_cli_used(
    username: str,
    password: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A CLI value on either flag triggers the deprecation log."""
    args = argparse.Namespace(username=username, password=password)
    with caplog.at_level("WARNING", logger="esphome_device_builder"):
        _warn_deprecated_credential_flags(args)
    assert any("DEPRECATION" in r.getMessage() for r in caplog.records)


def test_warn_deprecated_credential_flags_silent_when_env_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No warning when neither CLI flag is set."""
    args = argparse.Namespace(username="", password="")
    with caplog.at_level("WARNING", logger="esphome_device_builder"):
        _warn_deprecated_credential_flags(args)
    assert caplog.records == []


# ---------------------------------------------------------------------------
# DashboardSettings.parse_args — env-var lookup + precedence
# ---------------------------------------------------------------------------


def _parse(
    tmp_path: object,
    env: dict[str, str],
    *,
    username: str = "",
    password: str = "",
) -> DashboardSettings:
    """Build a ``DashboardSettings`` under a clean env populated from ``env``."""
    settings = DashboardSettings()
    with patch.dict(os.environ, env, clear=True):
        settings.parse_args(_ns(configuration=str(tmp_path), username=username, password=password))
    return settings


def test_parse_args_reads_env_vars(tmp_path: object) -> None:
    """``$ESPHOME_USERNAME`` / ``$ESPHOME_PASSWORD`` populate the dataclass."""
    settings = _parse(
        tmp_path,
        {"ESPHOME_USERNAME": "admin", "ESPHOME_PASSWORD": "hunter2"},
    )
    assert settings.username == "admin"
    assert settings.using_password is True
    # The hash is opaque but non-empty; ``check_password`` is the
    # canonical "did it round-trip" assertion.
    assert settings.check_password("admin", "hunter2") is True


def test_parse_args_reads_cli_flags(tmp_path: object) -> None:
    """``--username`` / ``--password`` populate the dataclass with no env."""
    settings = _parse(tmp_path, {}, username="admin", password="hunter2")
    assert settings.username == "admin"
    assert settings.using_password is True
    assert settings.check_password("admin", "hunter2") is True


def test_parse_args_cli_username_wins_over_env(tmp_path: object) -> None:
    """CLI ``--username`` overrides ``$ESPHOME_USERNAME``."""
    settings = _parse(
        tmp_path,
        {"ESPHOME_USERNAME": "from-env", "ESPHOME_PASSWORD": "hunter2"},
        username="from-cli",
    )
    assert settings.username == "from-cli"
    assert settings.check_password("from-cli", "hunter2") is True
    assert settings.check_password("from-env", "hunter2") is False


def test_parse_args_cli_password_wins_over_env(tmp_path: object) -> None:
    """CLI ``--password`` overrides ``$ESPHOME_PASSWORD``."""
    settings = _parse(
        tmp_path,
        {"ESPHOME_USERNAME": "admin", "ESPHOME_PASSWORD": "from-env"},
        password="from-cli",
    )
    assert settings.check_password("admin", "from-cli") is True
    assert settings.check_password("admin", "from-env") is False


def test_parse_args_mixes_cli_username_with_env_password(tmp_path: object) -> None:
    """``--username`` + ``$ESPHOME_PASSWORD`` is a valid combination."""
    settings = _parse(
        tmp_path,
        {"ESPHOME_PASSWORD": "hunter2"},
        username="admin",
    )
    assert settings.username == "admin"
    assert settings.using_password is True
    assert settings.check_password("admin", "hunter2") is True


def test_parse_args_mixes_env_username_with_cli_password(tmp_path: object) -> None:
    """``$ESPHOME_USERNAME`` + ``--password`` is a valid combination."""
    settings = _parse(
        tmp_path,
        {"ESPHOME_USERNAME": "admin"},
        password="hunter2",
    )
    assert settings.username == "admin"
    assert settings.using_password is True
    assert settings.check_password("admin", "hunter2") is True


# ---------------------------------------------------------------------------
# DashboardSettings.parse_args — bare $USERNAME / $PASSWORD must be ignored
# ---------------------------------------------------------------------------


def test_parse_args_ignores_bare_username_and_password(tmp_path: object) -> None:
    """Login-shell ``$USERNAME`` / ``$PASSWORD`` do not enable auth.

    The regression-fix this commit ships. Before the rename, this
    env state would have silently produced ``using_password=True``
    with ``username="jesse"`` — the OS user — and the operator's
    ``--password`` not being set would have left the dashboard
    requiring an unknowable password.
    """
    settings = _parse(
        tmp_path,
        {"USERNAME": "jesse", "PASSWORD": "shellsecret"},
    )
    assert settings.username == ""
    assert settings.using_password is False


def test_parse_args_bare_username_does_not_satisfy_esphome_username(tmp_path: object) -> None:
    """Bare ``$USERNAME`` doesn't substitute for ``$ESPHOME_USERNAME``.

    ``$ESPHOME_PASSWORD`` is set, ``$USERNAME`` is set (e.g. by the
    shell), but ``$ESPHOME_USERNAME`` is missing → the username side
    is empty, the password side is set, ``using_password`` falls
    back to False. The mismatch is real (and ``_validate_credentials``
    would reject it before we got here) but ``parse_args`` itself
    must also degrade safely if a caller bypasses the validator.
    """
    settings = _parse(
        tmp_path,
        {"USERNAME": "jesse", "ESPHOME_PASSWORD": "hunter2"},
    )
    assert settings.username == ""
    assert settings.using_password is False


def test_parse_args_esphome_username_wins_over_bare_username(tmp_path: object) -> None:
    """When both are present, ``$ESPHOME_USERNAME`` is used, not ``$USERNAME``.

    Pins that the lookup is name-specific — a future refactor to a
    shared "first-match-wins" helper that accidentally included
    the bare name in the candidate list would surface here.
    """
    settings = _parse(
        tmp_path,
        {
            "USERNAME": "shell-user",
            "ESPHOME_USERNAME": "real-admin",
            "ESPHOME_PASSWORD": "hunter2",
        },
    )
    assert settings.username == "real-admin"


def test_parse_args_no_credentials_disables_password(tmp_path: object) -> None:
    """No CLI, no env → unauthenticated startup."""
    settings = _parse(tmp_path, {})
    assert settings.username == ""
    assert settings.using_password is False
