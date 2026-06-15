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
from typing import TYPE_CHECKING, Any

from ..helpers.api import CommandError, api_command
from ..helpers.secrets_state import (
    is_wifi_unconfigured,
    read_secrets_yaml,
    write_wifi_secrets,
)
from ..models import (
    ErrorCode,
    OnboardingState,
    OnboardingStep,
    OnboardingStepId,
    OnboardingStepStatus,
    UserPreferences,
)
from ..models.onboarding import ONBOARDING_VERSION

if TYPE_CHECKING:
    from esphome_device_builder.controllers.config._preferences_store import PreferencesStore
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

    @property
    def _prefs(self) -> PreferencesStore:
        # config is created in start() before any onboarding command is served;
        # raise (not assert, which -O strips) if that invariant is ever broken.
        if self._db.config is None:  # pragma: no cover — config is always up post-start
            raise RuntimeError("config controller is not initialized")
        return self._db.config.prefs

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
        secrets = await loop.run_in_executor(None, read_secrets_yaml, self._db.settings.config_dir)
        prefs = self._prefs.snapshot()

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

        config_dir = self._db.settings.config_dir
        await self._db.write_secrets_locked(write_wifi_secrets, config_dir, ssid, password)
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

        def _bump(prefs: UserPreferences) -> None:
            # max(), not assign: a rollback from a future build must
            # not downgrade a higher stored acknowledgement.
            prefs.onboarding_completed_version = max(
                prefs.onboarding_completed_version, ONBOARDING_VERSION
            )

        self._prefs.mutate(_bump)
        return await self.get_state()
