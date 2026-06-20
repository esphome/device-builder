"""Resolve dashboard auth credentials from CLI flags and environment vars."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class ResolvedCredentials:
    """Outcome of resolving the dashboard username/password across all sources."""

    username: str
    password: str
    used_legacy: bool
    mismatch: bool


def resolve_credentials(
    username_arg: str,
    password_arg: str,
    environ: Mapping[str, str] = os.environ,
) -> ResolvedCredentials:
    """
    Resolve the dashboard credentials across flags, ``ESPHOME_*`` and legacy env.

    Precedence per credential: CLI flag, then ``$ESPHOME_USERNAME`` /
    ``$ESPHOME_PASSWORD``, then the deprecated bare ``$USERNAME`` /
    ``$PASSWORD`` pair. The bare pair is adopted only when the new scheme
    supplied nothing and ``$PASSWORD`` is set, so the OS-provided
    ``$USERNAME`` is never read on its own (it would otherwise promote the
    login-shell user to the dashboard username). ``mismatch`` is true when
    exactly one half of the resolved pair is set.
    """
    username = username_arg or environ.get("ESPHOME_USERNAME", "")
    password = password_arg or environ.get("ESPHOME_PASSWORD", "")
    used_legacy = False
    # Gate the legacy fallback on ``$PASSWORD`` specifically: it is not a
    # standard OS/shell variable, so its presence is the deliberate "I
    # configured legacy auth" signal. ``$USERNAME`` *is* the login user on
    # Linux/Windows, so it must never trigger the fallback or be read on its
    # own — doing so would silently promote the OS user to the dashboard
    # username (the footgun the ESPHOME_* rename fixed). On Windows
    # ``$USERNAME`` is always populated, so the gate there reduces to
    # "``$PASSWORD`` is set" and the adopted username is the OS login name;
    # that fails safe (more locked down, and the deprecation banner fires),
    # not open.
    if not username and not password and environ.get("PASSWORD"):
        username = environ.get("USERNAME", "")
        password = environ.get("PASSWORD", "")
        used_legacy = bool(username and password)
    return ResolvedCredentials(
        username=username,
        password=password,
        used_legacy=used_legacy,
        mismatch=bool(username) != bool(password),
    )
