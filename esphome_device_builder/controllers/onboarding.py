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
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from esphome.util import list_yaml_files

from ..helpers.api import CommandError, api_command
from ..helpers.secrets_state import (
    is_wifi_unconfigured,
    read_secrets_yaml,
    write_wifi_secrets,
)
from ..models import (
    ErrorCode,
    ExperienceLevel,
    OnboardingState,
    OnboardingStep,
    OnboardingStepId,
    OnboardingStepStatus,
    UserPreferences,
)
from ..models.onboarding import ONBOARDING_VERSION
from .config.settings import _DASHBOARD_SENTINEL_FILE

if TYPE_CHECKING:
    from esphome_device_builder.controllers.config._preferences_store import PreferencesStore
    from esphome_device_builder.device_builder import DeviceBuilder

_LOGGER = logging.getLogger(__name__)


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

        The step list is environment- and preference-aware: the
        use-case step only on non-HA installs, the Wi-Fi step only
        when not remote-compute-only. Each status is computed from
        live data; the frontend surfaces the wizard on any pending
        step or a newer version.
        """
        settings = self._db.settings
        prefs = self._prefs.snapshot()
        loop = asyncio.get_running_loop()
        secrets = await loop.run_in_executor(None, read_secrets_yaml, settings.config_dir)
        return _compute_state(secrets, prefs, on_ha_addon=settings.on_ha_addon)

    async def migrate_preexisting_install(self) -> None:
        """
        Default a pre-existing install to the EXPERT experience, once.

        Installs that completed an earlier onboarding or already hold
        device YAMLs predate the experience picker; mark them EXPERT
        users and acknowledge onboarding so the wizard never auto-pops.
        Idempotent — a no-op once ``experience_level`` is set.
        """
        prefs = self._prefs.snapshot()
        if prefs.experience_level is not None:
            return
        has_configs = False
        if prefs.onboarding_completed_version == 0:
            loop = asyncio.get_running_loop()
            has_configs = await loop.run_in_executor(
                None, _has_device_configs, self._db.settings.config_dir
            )
        if _should_migrate_preexisting(prefs, has_device_configs=has_configs):
            self._prefs.mutate(_mark_preexisting)

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
        self._prefs.mutate(_acknowledge_current_version)
        return await self.get_state()


def _compute_state(
    secrets: dict | None, prefs: UserPreferences, *, on_ha_addon: bool
) -> OnboardingState:
    """
    Assemble the environment- and preference-aware onboarding step list.

    *secrets* and *prefs* are read by the caller (secrets off the loop, prefs
    from the RAM-canonical store) so this stays pure.
    """
    experience_done = _status(done=prefs.experience_level is not None)

    steps: list[OnboardingStep] = []
    # Use-case (remote-compute?) is a non-HA question; HA users manage
    # devices in Home Assistant. Its status tracks the experience pick,
    # which the wizard always answers in the same pass.
    if not on_ha_addon:
        steps.append(OnboardingStep(id=OnboardingStepId.USE_CASE, status=experience_done))
    steps.append(OnboardingStep(id=OnboardingStepId.EXPERIENCE_LEVEL, status=experience_done))
    if not prefs.remote_compute_only:
        steps.append(
            OnboardingStep(
                id=OnboardingStepId.WIFI_CREDENTIALS,
                status=_status(done=not is_wifi_unconfigured(secrets)),
            )
        )

    return OnboardingState(
        current_version=ONBOARDING_VERSION,
        completed_version=prefs.onboarding_completed_version,
        steps=steps,
    )


def _should_migrate_preexisting(prefs: UserPreferences, *, has_device_configs: bool) -> bool:
    """Whether a pre-existing install should default to the EXPERT experience.

    No-op once an experience is chosen; a fresh install with no device YAMLs and
    no prior onboarding is left alone so the wizard still runs.
    """
    if prefs.experience_level is not None:
        return False
    return prefs.onboarding_completed_version > 0 or has_device_configs


def _mark_preexisting(p: UserPreferences) -> None:
    """Mark *p* an EXPERT user; acknowledge only if onboarding was already done."""
    p.experience_level = ExperienceLevel.EXPERT
    # Only acknowledge onboarding for installs that already completed it, so a
    # prior Wi-Fi save or decline is respected. An install known only by its
    # device YAML stays un-acknowledged, so a missing-Wi-Fi prompt still surfaces.
    if p.onboarding_completed_version > 0:
        _acknowledge_current_version(p)


def _acknowledge_current_version(prefs: UserPreferences) -> None:
    """
    Raise the acknowledged onboarding version to current, never downgrading.

    max(), not assign: a rollback from a future build must not downgrade a
    higher stored acknowledgement.
    """
    prefs.onboarding_completed_version = max(prefs.onboarding_completed_version, ONBOARDING_VERSION)


def _has_device_configs(config_dir: Path) -> bool:
    """
    Return True when the config dir holds any user device YAML.

    Uses the canonical ``list_yaml_files`` rule (.yaml + .yml, secrets
    and dotfiles excluded) so it can't drift from the device scanner;
    only the dashboard sentinel needs excluding on top.

    A missing dir is a genuinely fresh install (return False). A dir that
    exists but can't be read fails *safe for existing users*: assume it
    holds configs so a transient read error can't reclassify a real
    install as fresh and re-pop the wizard.
    """
    try:
        return any(p.name != _DASHBOARD_SENTINEL_FILE for p in list_yaml_files([config_dir]))
    except FileNotFoundError:
        return False
    except OSError:
        _LOGGER.warning(
            "Could not scan %s for device configs; assuming pre-existing install",
            config_dir,
            exc_info=True,
        )
        return True


def _status(*, done: bool) -> OnboardingStepStatus:
    """Map a done-ness boolean to the step status enum."""
    return OnboardingStepStatus.DONE if done else OnboardingStepStatus.PENDING
