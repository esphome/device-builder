"""
Dashboard onboarding controller.

Surfaces first-run setup the user needs to complete to have a
working dashboard (use-case + experience level). Wi-Fi credentials
are collected per-device in the create wizard, not here. Designed
to grow as we add more guidance (Home Assistant addon hand-off,
encryption-key defaults, …).

Each step's ``status`` is computed from live prefs every time
``get_state`` is called — never persisted in the step list.
Acknowledgement is tracked separately via
``onboarding_completed_version`` in user preferences so a future
release can bump :data:`ONBOARDING_VERSION` to re-prompt users
who completed an earlier flow.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from esphome.util import list_yaml_files

from ..helpers.api import api_command
from ..helpers.async_ import run_in_executor
from ..models import (
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

        The step list is environment-aware: the use-case step only on
        non-HA installs. The frontend surfaces the wizard on any pending
        step or a newer version.
        """
        settings = self._db.settings
        prefs = self._prefs.snapshot()
        return _compute_state(prefs, on_ha_addon=settings.on_ha_addon)

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
            has_configs = await run_in_executor(_has_device_configs, self._db.settings.config_dir)
        if _should_migrate_preexisting(prefs, has_device_configs=has_configs):
            self._prefs.mutate(_mark_preexisting)

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


def _compute_state(prefs: UserPreferences, *, on_ha_addon: bool) -> OnboardingState:
    """
    Assemble the environment-aware onboarding step list.

    *prefs* is read by the caller (from the RAM-canonical store) so this
    stays pure. Wi-Fi credentials are collected per-device in the create
    wizard, not here.
    """
    experience_done = _status(done=prefs.experience_level is not None)

    steps: list[OnboardingStep] = []
    # Use-case (remote-compute?) is a non-HA question; HA users manage
    # devices in Home Assistant. Its status tracks the experience pick,
    # which the wizard always answers in the same pass.
    if not on_ha_addon:
        steps.append(OnboardingStep(id=OnboardingStepId.USE_CASE, status=experience_done))
    steps.append(OnboardingStep(id=OnboardingStepId.EXPERIENCE_LEVEL, status=experience_done))

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
