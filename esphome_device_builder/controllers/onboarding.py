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

from esphome import yaml_util
from esphome.helpers import write_file as atomic_write_file

from ..helpers.api import CommandError, api_command
from ..helpers.secrets_state import is_wifi_unconfigured
from ..models import (
    ErrorCode,
    OnboardingState,
    OnboardingStep,
    OnboardingStepId,
    OnboardingStepStatus,
    UserPreferences,
)
from ..models.onboarding import ONBOARDING_VERSION
from .config import load_preferences, save_preferences

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
        config_dir = self._db.settings.config_dir

        secrets, prefs = await asyncio.gather(
            loop.run_in_executor(None, _read_secrets, config_dir),
            loop.run_in_executor(None, load_preferences, config_dir),
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
        ssid = ssid.strip()
        if not ssid:
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

        current = await loop.run_in_executor(None, load_preferences, config_dir)
        if current.onboarding_completed_version != ONBOARDING_VERSION:
            current_dict = current.to_dict()
            current_dict["onboarding_completed_version"] = ONBOARDING_VERSION
            updated = UserPreferences.from_dict(current_dict)
            await loop.run_in_executor(None, save_preferences, config_dir, updated)
        return await self.get_state()


def _read_secrets(config_dir: Path) -> dict | None:
    """Read ``secrets.yaml`` into a plain dict, returning ``None`` on any failure.

    Mirrors the silent-fallback contract of ``ConfigController.get_secrets``
    so a malformed file (or one we can't open) reads as
    "unconfigured" instead of raising and breaking onboarding.
    """
    secrets_path = config_dir / "secrets.yaml"
    if not secrets_path.exists():
        return None
    try:
        data = yaml_util.load_yaml(secrets_path)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


# ``key: value`` line where ``key`` is the captured group. Permissive
# on whitespace + value shape so we match both ``wifi_ssid: ""`` and
# ``wifi_ssid: REPLACE_WITH_…`` and ``wifi_ssid:`` (empty raw).
_SECRET_LINE_RE = re.compile(r"^(\s*)([a-zA-Z_][\w]*)\s*:.*$")


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
    original = secrets_path.read_text() if secrets_path.exists() else ""

    updated = _replace_or_append_secret(
        _replace_or_append_secret(original, "wifi_ssid", ssid),
        "wifi_password",
        password,
    )
    atomic_write_file(secrets_path, updated)


def _replace_or_append_secret(content: str, key: str, value: str) -> str:
    """
    Set ``key`` to ``value`` in YAML *content*, in place.

    Replaces the value on the first line whose key matches; if no
    such line exists, appends ``key: "value"`` at the end (with a
    trailing newline if needed). Comments and other lines are
    untouched.
    """
    encoded = _quote_yaml_string(value)
    lines = content.split("\n")
    for i, line in enumerate(lines):
        m = _SECRET_LINE_RE.match(line)
        if m and m.group(2) == key:
            lines[i] = f"{m.group(1)}{key}: {encoded}"
            return "\n".join(lines)
    # Append. Make sure we don't double a trailing newline.
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
