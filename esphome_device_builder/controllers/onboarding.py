"""
Dashboard onboarding controller.

Surfaces first-run setup the user needs to complete to have a
working dashboard. Currently one step (Wi-Fi credentials);
designed to grow as we add more guidance (Home Assistant addon
hand-off, encryption-key defaults, …).

Each step's ``status`` is computed from live on-disk state every
time ``get_state`` is called — never persisted, never derived from
user prefs. The badge in the frontend kebab menu accordingly
clears the moment the user configures the underlying data, even if
they did so outside the wizard (manual ``secrets.yaml`` edit).
Acknowledgement is tracked separately via
``onboarding_completed_version`` in user preferences so a future
release can bump :data:`ONBOARDING_VERSION` to re-prompt users
who completed an earlier flow.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from esphome.helpers import write_file as atomic_write_file

from ..helpers.api import CommandError, api_command
from ..helpers.secrets_state import is_wifi_unconfigured, read_secrets_yaml
from ..models import (
    ErrorCode,
    OnboardingState,
    OnboardingStep,
    OnboardingStepId,
    OnboardingStepStatus,
    UserPreferences,
)
from ..models.onboarding import ONBOARDING_VERSION
from .config import load_preferences, mutate_preferences

if TYPE_CHECKING:
    from esphome_device_builder.device_builder import DeviceBuilder


# Cap inputs at the same length ESPHome's own validators enforce —
# ``cv.ssid`` (32 chars) and the WPA password validator (64 chars).
# Catches malformed input early so the user sees a clean
# ``CommandError`` instead of a downstream YAML-encode surprise.
_MAX_SSID_LEN = 32
_MAX_WIFI_PASSWORD_LEN = 64


class OnboardingController:
    """WebSocket endpoints for the dashboard onboarding flow."""

    def __init__(self, db: DeviceBuilder) -> None:
        self._db = db

    @api_command("onboarding/get_state")
    async def get_state(self, **kwargs: Any) -> OnboardingState:
        """
        Return the current onboarding snapshot.

        Computes each step's status from live data, then reads the
        user's last-acknowledged version from preferences. The
        frontend combines the two to decide whether to surface the
        wizard (any pending step OR new version available).
        """
        loop = asyncio.get_running_loop()
        secrets, prefs = await loop.run_in_executor(
            None, _read_secrets_and_prefs, self._db.settings.config_dir
        )

        return OnboardingState(
            current_version=ONBOARDING_VERSION,
            completed_version=prefs.onboarding_completed_version,
            steps=[
                OnboardingStep(
                    id=OnboardingStepId.WIFI_CREDENTIALS,
                    status=OnboardingStepStatus.PENDING
                    if is_wifi_unconfigured(secrets)
                    else OnboardingStepStatus.DONE,
                ),
            ],
        )

    @api_command("onboarding/set_wifi_credentials")
    async def set_wifi_credentials(
        self,
        *,
        ssid: str,
        password: str = "",
        **kwargs: Any,
    ) -> OnboardingState:
        """
        Update ``wifi_ssid`` / ``wifi_password`` in ``secrets.yaml``.

        Validates inputs against ESPHome's own length limits so a
        malformed value can't slip through to the next ``compile``.
        Preserves any other secret keys + the file's comments via a
        line-based rewrite.
        """
        # The WS layer doesn't enforce JSON value types, so a
        # client sending ``ssid: 42`` or ``password: null`` would
        # otherwise reach ``.strip()`` / ``len()`` and surface as
        # an ``INTERNAL_ERROR`` (AttributeError / TypeError).
        # Reject up-front with a clean ``INVALID_ARGS`` so the
        # frontend can render the error inline in the wizard.
        if not isinstance(ssid, str):
            raise CommandError(ErrorCode.INVALID_ARGS, "SSID must be a string.")
        if not isinstance(password, str):
            raise CommandError(ErrorCode.INVALID_ARGS, "Password must be a string.")
        # IEEE 802.11 SSIDs may legally contain leading or trailing
        # whitespace, so don't mutate the user's input — they may
        # have an awkwardly-named network on purpose. Reject only
        # the all-whitespace / empty case (which can't address a
        # real network) and use the original ``ssid`` for the
        # length check + the file write.
        if not ssid.strip():
            raise CommandError(ErrorCode.INVALID_ARGS, "SSID can't be empty.")
        if len(ssid) > _MAX_SSID_LEN:
            raise CommandError(
                ErrorCode.INVALID_ARGS,
                f"SSID can't be longer than {_MAX_SSID_LEN} characters.",
            )
        if len(password) > _MAX_WIFI_PASSWORD_LEN:
            raise CommandError(
                ErrorCode.INVALID_ARGS,
                f"Password can't be longer than {_MAX_WIFI_PASSWORD_LEN} characters.",
            )
        # Reject every control character except TAB. The line-based
        # secrets.yaml rewrite emits the value on a single line, so
        # ``\n`` / ``\r`` inject extra YAML lines and ``\0`` would
        # terminate the file early on read; broader C0 / DEL bytes
        # (BEL, ESC, …) make PyYAML reject the result on the next
        # ``read_secrets_yaml``, silently flipping onboarding back
        # to PENDING after a successful "save". Block the whole
        # control range up-front so that path can't be reached
        # — TAB stays allowed because it's the one whitespace
        # ESPHome's own ``cv.string_strict`` accepts.
        for label, value in (("SSID", ssid), ("Password", password)):
            if any(c != "\t" and (ord(c) < 0x20 or ord(c) == 0x7F) for c in value):
                raise CommandError(
                    ErrorCode.INVALID_ARGS,
                    f"{label} can't contain control characters.",
                )

        loop = asyncio.get_running_loop()
        config_dir = self._db.settings.config_dir
        await loop.run_in_executor(None, _write_wifi_secrets, config_dir, ssid, password)
        return await self.get_state()

    @api_command("onboarding/mark_acknowledged")
    async def mark_acknowledged(self, **kwargs: Any) -> OnboardingState:
        """
        Record that the user has finished the current onboarding flow.

        Sets ``onboarding_completed_version`` to
        :data:`ONBOARDING_VERSION` in user preferences. Future
        releases that add new steps bump that constant; existing
        users with a lower stored value will be re-prompted.
        """
        loop = asyncio.get_running_loop()
        config_dir = self._db.settings.config_dir

        def _bump(prefs: UserPreferences) -> None:
            # max(), not assign: a rollback from a future build must
            # not downgrade a higher stored acknowledgement.
            prefs.onboarding_completed_version = max(
                prefs.onboarding_completed_version, ONBOARDING_VERSION
            )

        await loop.run_in_executor(None, mutate_preferences, config_dir, _bump)
        return await self.get_state()


# ``key: value`` line. Captures: 1=indent, 2=key, 3=trailing
# ``  # comment`` (with at least one space before the ``#``).
# Permissive on value shape so we match both ``wifi_ssid: ""``
# and bare ``wifi_ssid:`` — the value itself is discarded on
# rewrite, only indent / key / trailing comment carry over.
#
# Known limitation: a ``#`` *inside a quoted value* preceded by
# whitespace (e.g. ``wifi_ssid: "foo # bar"``) is mis-parsed as
# a trailing comment. The rewrite still produces valid YAML
# because the new value is re-quoted, but the spurious tail is
# preserved as a comment. See the dedicated regression test in
# ``tests/test_onboarding_controller.py``. Realistic impact is
# low — ``#`` in SSIDs is uncommon and the user's hand-edit has
# to land before they re-run the wizard.
_SECRET_LINE_RE = re.compile(r"^(\s*)([a-zA-Z_]\w*)\s*:[^#\n]*?(\s+#.*)?$")


def _read_secrets_and_prefs(config_dir: Path) -> tuple[dict | None, UserPreferences]:
    """
    Read both ``secrets.yaml`` and user preferences in one executor hop.

    Both are quick disk reads from the same config dir, so a single
    executor job is cheaper than two. ``get_state`` runs on every
    page load + after every secrets save, so the saved hop matters.
    """
    return read_secrets_yaml(config_dir), load_preferences(config_dir)


def _write_wifi_secrets(config_dir: Path, ssid: str, password: str) -> None:
    """
    Update ``wifi_ssid`` and ``wifi_password`` in ``secrets.yaml`` in place.

    Line-based rewrite preserves comments and any other secrets the
    user has added. Falls back to creating the file with just the
    two keys if it doesn't exist (the bootstrap should have created
    it on startup, but a user who deleted it shouldn't be stuck
    here).
    """
    secrets_path = config_dir / "secrets.yaml"
    original = secrets_path.read_text(encoding="utf-8") if secrets_path.exists() else ""

    updated = _replace_or_append_secret(
        _replace_or_append_secret(original, "wifi_ssid", ssid),
        "wifi_password",
        password,
    )
    atomic_write_file(secrets_path, updated)


def _replace_or_append_secret(content: str, key: str, value: str) -> str:
    """
    Set ``key`` to ``value`` in YAML *content*, in place.

    Replaces the value on **every** line whose key matches — a
    duplicated key in ``secrets.yaml`` is malformed (PyYAML keeps
    only the last on read), but writing only the first match
    would leave the stale duplicate as the live value and
    onboarding would stay PENDING after a "successful" save. Any
    inline ``# comment`` trailing the matched line is preserved
    so a power-user with ``wifi_ssid: home  # Apt 4B router``
    keeps the annotation. If no line matches, appends
    ``key: "value"`` at the end with a trailing newline.
    """
    encoded = _quote_yaml_string(value)
    lines = content.split("\n")
    matched = False
    for i, line in enumerate(lines):
        m = _SECRET_LINE_RE.match(line)
        if m and m.group(2) == key:
            trailing_comment = m.group(3) or ""
            lines[i] = f"{m.group(1)}{key}: {encoded}{trailing_comment}"
            matched = True
    if matched:
        return "\n".join(lines)
    # Append. Empty input gets the line on its own (no leading
    # blank); any other input gets a single ``\n`` separator if it
    # doesn't already end with one.
    if not content:
        return f"{key}: {encoded}\n"
    if not content.endswith("\n"):
        content = content + "\n"
    return f"{content}{key}: {encoded}\n"


def _quote_yaml_string(value: str) -> str:
    r"""
    Quote *value* as a YAML double-quoted scalar.

    Always uses double quotes so the round-trip stays predictable
    regardless of what characters the user typed. Escapes the two
    characters that have meaning inside double-quoted strings
    (``\`` and ``"``) — everything else passes through verbatim.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
