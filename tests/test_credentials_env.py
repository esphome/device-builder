"""Tests for ``ESPHOME_USERNAME`` / ``ESPHOME_PASSWORD`` env-var handling.

Both layers share one resolver (``helpers.credentials.resolve_credentials``):

* ``__main__._validate_credentials`` — the CLI-time mismatch guard
  that errors out before ``DashboardSettings`` is even built when
  one half of the credential pair is set without the other.
* ``DashboardSettings.parse_args`` — the actual lookup that
  populates ``settings.username`` / ``settings.password_hash``.

Precedence per credential: ``--flag`` > ``$ESPHOME_*`` >
deprecated bare ``$USERNAME`` / ``$PASSWORD`` pair. The bare pair is
kept for back-compat with pre-rename dashboards (warned about at
startup via ``_warn_legacy_credential_env``) but is adopted **only
as a pair, gated on ``$PASSWORD``** — the OS-set ``$USERNAME`` is
never read on its own.
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
    _warn_legacy_credential_env,
)
from esphome_device_builder.controllers.config import DashboardSettings
from esphome_device_builder.helpers.credentials import resolve_credentials


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
        # Bare ``USERNAME`` / ``PASSWORD`` together — adopted as a
        # back-compat pair (both halves present), so it's a matched
        # pair, not a mismatch.
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
        # still error: the new scheme supplied a password, so the
        # legacy pair is not adopted, and the bare ``$USERNAME`` is
        # never read on its own — only one half is set.
        ({"USERNAME": "jesse"}, "", "hunter2"),
        # Symmetric: bare ``PASSWORD`` + real ``--username``. The new
        # scheme supplied a username, so the legacy pair isn't adopted.
        ({"PASSWORD": "shellsecret"}, "admin", ""),
        # Legacy ``$PASSWORD`` with no ``$USERNAME`` (atypical — the
        # getting-started guide sets both). Adopted as a pair, but the
        # username half is empty, so it's a mismatch, not silent auth.
        ({"PASSWORD": "shellsecret"}, "", ""),
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
# DashboardSettings.parse_args — legacy bare $USERNAME / $PASSWORD fallback
# ---------------------------------------------------------------------------


def test_parse_args_adopts_legacy_bare_pair(tmp_path: object) -> None:
    """A pre-rename ``$USERNAME`` + ``$PASSWORD`` pair keeps the dashboard protected.

    The back-compat path: an operator who set the legacy bare names
    stays authenticated after upgrading into device-builder (warned
    about separately via ``_warn_legacy_credential_env``).
    """
    settings = _parse(
        tmp_path,
        {"USERNAME": "jesse", "PASSWORD": "shellsecret"},
    )
    assert settings.username == "jesse"
    assert settings.using_password is True
    assert settings.check_password("jesse", "shellsecret") is True


def test_parse_args_ignores_bare_username_without_password(tmp_path: object) -> None:
    """A lone OS-set ``$USERNAME`` (no ``$PASSWORD``) never enables auth.

    The footgun the pair-gating avoids: ``$USERNAME`` is the login
    user on Linux/Windows, so reading it on its own would promote the
    OS user to the dashboard username. Gated on ``$PASSWORD``, this
    starts unauthenticated.
    """
    settings = _parse(tmp_path, {"USERNAME": "jesse"})
    assert settings.username == ""
    assert settings.using_password is False


def test_parse_args_bare_username_does_not_satisfy_esphome_username(tmp_path: object) -> None:
    """Bare ``$USERNAME`` doesn't substitute for ``$ESPHOME_USERNAME``.

    ``$ESPHOME_PASSWORD`` is set, ``$USERNAME`` is set (e.g. by the
    shell), but ``$ESPHOME_USERNAME`` is missing → the new scheme
    supplied a password, so the legacy pair isn't adopted and the bare
    ``$USERNAME`` is not read; the username side stays empty,
    ``using_password`` falls back to False. ``_validate_credentials``
    rejects this before we get here, but ``parse_args`` must also
    degrade safely if a caller bypasses the validator.
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


# ---------------------------------------------------------------------------
# resolve_credentials — precedence + legacy fallback (the shared resolver)
# ---------------------------------------------------------------------------


def test_resolve_cli_flags_win_over_every_env() -> None:
    """CLI flags beat both the new and legacy env vars."""
    r = resolve_credentials(
        "cli-user",
        "cli-pass",
        {
            "ESPHOME_USERNAME": "env-user",
            "ESPHOME_PASSWORD": "env-pass",
            "USERNAME": "shell-user",
            "PASSWORD": "shell-pass",
        },
    )
    assert (r.username, r.password) == ("cli-user", "cli-pass")
    assert r.used_legacy is False
    assert r.mismatch is False


def test_resolve_esphome_env_wins_over_legacy() -> None:
    """``$ESPHOME_*`` beats the bare names; the legacy pair isn't adopted."""
    r = resolve_credentials(
        "",
        "",
        {
            "ESPHOME_USERNAME": "admin",
            "ESPHOME_PASSWORD": "hunter2",
            "USERNAME": "shell-user",
            "PASSWORD": "shell-pass",
        },
    )
    assert (r.username, r.password) == ("admin", "hunter2")
    assert r.used_legacy is False


def test_resolve_adopts_legacy_pair() -> None:
    """Bare ``$USERNAME`` + ``$PASSWORD`` with no new scheme → adopted pair."""
    r = resolve_credentials("", "", {"USERNAME": "jesse", "PASSWORD": "shellsecret"})
    assert (r.username, r.password) == ("jesse", "shellsecret")
    assert r.used_legacy is True
    assert r.mismatch is False


def test_resolve_ignores_lone_bare_username() -> None:
    """A lone OS-set ``$USERNAME`` (no ``$PASSWORD``) is never read."""
    r = resolve_credentials("", "", {"USERNAME": "jesse"})
    assert (r.username, r.password) == ("", "")
    assert r.used_legacy is False
    assert r.mismatch is False


def test_resolve_lone_bare_password_is_mismatch() -> None:
    """Bare ``$PASSWORD`` with no username resolves to a half-set mismatch."""
    r = resolve_credentials("", "", {"PASSWORD": "shellsecret"})
    assert r.used_legacy is False
    assert r.mismatch is True


def test_resolve_does_not_adopt_legacy_when_new_username_set() -> None:
    """A new-scheme username present blocks legacy adoption (stays a mismatch)."""
    r = resolve_credentials("", "", {"ESPHOME_USERNAME": "admin", "PASSWORD": "shellsecret"})
    assert (r.username, r.password) == ("admin", "")
    assert r.used_legacy is False
    assert r.mismatch is True


# ---------------------------------------------------------------------------
# _warn_legacy_credential_env — loud banner only when the legacy pair is used
# ---------------------------------------------------------------------------


def test_warn_legacy_credential_env_fires_on_legacy_pair(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Adopting the bare pair logs a deprecation banner naming the new vars."""
    args = argparse.Namespace(username="", password="")
    with (
        patch.dict(os.environ, {"USERNAME": "jesse", "PASSWORD": "shellsecret"}, clear=True),
        caplog.at_level("WARNING", logger="esphome_device_builder"),
    ):
        _warn_legacy_credential_env(args)
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "DEPRECATION" in msg
    assert "$ESPHOME_USERNAME" in msg
    assert "$ESPHOME_PASSWORD" in msg


def test_warn_legacy_credential_env_silent_on_new_scheme(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No banner when the new ``$ESPHOME_*`` scheme supplies the credentials."""
    args = argparse.Namespace(username="", password="")
    with (
        patch.dict(
            os.environ,
            {"ESPHOME_USERNAME": "admin", "ESPHOME_PASSWORD": "hunter2"},
            clear=True,
        ),
        caplog.at_level("WARNING", logger="esphome_device_builder"),
    ):
        _warn_legacy_credential_env(args)
    assert caplog.records == []
