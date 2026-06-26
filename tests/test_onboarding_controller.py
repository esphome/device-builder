"""Tests for ``OnboardingController`` — the dashboard onboarding flow.

Covers ``get_state``, ``mark_acknowledged``, and the pre-existing-install
migration against a per-test ``tmp_path`` config dir. Wi-Fi collection
moved to the create wizard / ``config/set_wifi_credentials``; those tests
live in ``test_config_controller``. The controller is constructed via
``__new__`` so we can stub ``self._db.settings`` without driving the full
``DeviceBuilder`` init chain.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

from esphome_device_builder.controllers.config._preferences_store import PreferencesStore
from esphome_device_builder.controllers.onboarding import (
    OnboardingController,
    _has_device_configs,
    _mark_preexisting,
    _should_migrate_preexisting,
)
from esphome_device_builder.models.onboarding import (
    ONBOARDING_VERSION,
    OnboardingState,
    OnboardingStepId,
    OnboardingStepStatus,
)
from esphome_device_builder.models.preferences import (
    ExperienceLevel,
    Theme,
    UserPreferences,
)

from .conftest import wire_secrets_writer


def _make_controller(
    config_dir: Path, *, on_ha_addon: bool = False, prefs: UserPreferences | None = None
) -> OnboardingController:
    controller = OnboardingController.__new__(OnboardingController)
    controller._db = MagicMock()
    controller._db.settings.config_dir = config_dir
    controller._db.settings.absolute_config_dir = config_dir.resolve()
    controller._db.settings.on_ha_addon = on_ha_addon
    controller._db.secrets_write_lock = asyncio.Lock()
    wire_secrets_writer(controller._db)
    # RAM-canonical prefs store seeded in RAM; mutations stay in RAM (the
    # debounce timer never fires within the test), asserted via the snapshot.
    store = PreferencesStore(config_dir, lambda _cb: None)
    store._state = prefs if prefs is not None else UserPreferences()
    controller._db.config.prefs = store
    return controller


def _step(state: OnboardingState, step_id: OnboardingStepId) -> OnboardingStepStatus | None:
    """Status of one step by id, or None when the step isn't in the list."""
    return next((s.status for s in state.steps if s.id == step_id), None)


def _write_secrets(config_dir: Path, content: str) -> None:
    (config_dir / "secrets.yaml").write_text(content)


# ---------------------------------------------------------------------------
# get_state — environment-aware step list (Wi-Fi moved to the create wizard)
# ---------------------------------------------------------------------------


async def test_get_state_version_baseline(tmp_path: Path) -> None:
    """Fresh install reports the current version and a zero acknowledgement."""
    controller = _make_controller(tmp_path)
    state = await controller.get_state()
    assert state.current_version == ONBOARDING_VERSION
    assert state.completed_version == 0


async def test_get_state_non_ha_includes_use_case_step(tmp_path: Path) -> None:
    """Non-HA installs ask the remote-compute use-case question; no Wi-Fi step."""
    controller = _make_controller(tmp_path, on_ha_addon=False)
    state = await controller.get_state()
    ids = [s.id for s in state.steps]
    assert ids == [OnboardingStepId.USE_CASE, OnboardingStepId.EXPERIENCE_LEVEL]
    assert _step(state, OnboardingStepId.USE_CASE) == OnboardingStepStatus.PENDING
    assert _step(state, OnboardingStepId.EXPERIENCE_LEVEL) == OnboardingStepStatus.PENDING


async def test_get_state_ha_addon_omits_use_case_step(tmp_path: Path) -> None:
    """HA addon manages devices in HA, so no use-case question."""
    controller = _make_controller(tmp_path, on_ha_addon=True)
    state = await controller.get_state()
    assert [s.id for s in state.steps] == [OnboardingStepId.EXPERIENCE_LEVEL]


async def test_get_state_experience_set_marks_use_case_and_experience_done(
    tmp_path: Path,
) -> None:
    """Picking an experience level completes both leading steps."""
    controller = _make_controller(
        tmp_path, on_ha_addon=False, prefs=UserPreferences(experience_level=ExperienceLevel.EXPERT)
    )
    state = await controller.get_state()
    assert _step(state, OnboardingStepId.USE_CASE) == OnboardingStepStatus.DONE
    assert _step(state, OnboardingStepId.EXPERIENCE_LEVEL) == OnboardingStepStatus.DONE


# ---------------------------------------------------------------------------
# mark_acknowledged
# ---------------------------------------------------------------------------


async def test_mark_acknowledged_persists_current_version(tmp_path: Path) -> None:
    controller = _make_controller(tmp_path)
    state = await controller.mark_acknowledged()
    assert state.completed_version == ONBOARDING_VERSION
    assert controller._prefs.snapshot().onboarding_completed_version == ONBOARDING_VERSION


async def test_mark_acknowledged_is_idempotent(tmp_path: Path) -> None:
    controller = _make_controller(tmp_path)
    await controller.mark_acknowledged()
    state = await controller.mark_acknowledged()
    assert state.completed_version == ONBOARDING_VERSION


async def test_mark_acknowledged_keeps_other_pref_fields(tmp_path: Path) -> None:
    """Acknowledging touches only the version, leaving an unrelated field intact."""
    controller = _make_controller(tmp_path, prefs=UserPreferences(theme=Theme.DARK))
    await controller.mark_acknowledged()
    snap = controller._prefs.snapshot()
    assert snap.onboarding_completed_version == ONBOARDING_VERSION
    assert snap.theme == Theme.DARK


async def test_mark_acknowledged_does_not_downgrade_a_higher_stored_version(
    tmp_path: Path,
) -> None:
    """Don't lose a future-build acknowledgement on rollback.

    A user who briefly ran a future build with a higher
    ``ONBOARDING_VERSION`` and then rolled back keeps the higher stored
    value — otherwise they'd be re-prompted on the next upgrade for steps
    they've already done.
    """
    controller = _make_controller(
        tmp_path, prefs=UserPreferences(onboarding_completed_version=ONBOARDING_VERSION + 5)
    )
    state = await controller.mark_acknowledged()
    assert state.completed_version == ONBOARDING_VERSION + 5


# ---------------------------------------------------------------------------
# migrate_preexisting_install
# ---------------------------------------------------------------------------


async def test_migrate_acknowledged_install_becomes_expert_and_stays_acknowledged(
    tmp_path: Path,
) -> None:
    """An install that completed an earlier onboarding keeps its acknowledgement.

    Their prior Wi-Fi save / decline stands, so onboarding is bumped to current.
    """
    controller = _make_controller(tmp_path, prefs=UserPreferences(onboarding_completed_version=1))
    await controller.migrate_preexisting_install()
    prefs = controller._prefs.snapshot()
    assert prefs.experience_level == ExperienceLevel.EXPERT
    assert prefs.onboarding_completed_version == ONBOARDING_VERSION


async def test_migrate_device_yaml_install_stays_unacknowledged(tmp_path: Path) -> None:
    """A config-only install that never onboarded gets EXPERT but no acknowledgement.

    Leaving ``onboarding_completed_version`` at 0 lets a missing-Wi-Fi prompt
    still fire for these users.
    """
    (tmp_path / "living-room.yaml").write_text("esphome:\n  name: living-room\n")
    controller = _make_controller(tmp_path)
    await controller.migrate_preexisting_install()
    prefs = controller._prefs.snapshot()
    assert prefs.experience_level == ExperienceLevel.EXPERT
    assert prefs.onboarding_completed_version == 0


async def test_migrate_install_with_yml_extension_becomes_expert(tmp_path: Path) -> None:
    """``.yml`` is an equally valid config extension; it must trigger migration too."""
    (tmp_path / "bedroom.yml").write_text("esphome:\n  name: bedroom\n")
    controller = _make_controller(tmp_path)
    await controller.migrate_preexisting_install()
    prefs = controller._prefs.snapshot()
    assert prefs.experience_level == ExperienceLevel.EXPERT
    assert prefs.onboarding_completed_version == 0


def test_should_migrate_preexisting_decision() -> None:
    """The YAML-default decision: skip a chosen experience and a bare fresh install."""
    # Already chose an experience → never migrate.
    assert not _should_migrate_preexisting(
        UserPreferences(experience_level=ExperienceLevel.BEGINNER), has_device_configs=True
    )
    # Unchosen + acknowledged earlier onboarding → migrate.
    assert _should_migrate_preexisting(
        UserPreferences(onboarding_completed_version=1), has_device_configs=False
    )
    # Unchosen + has device YAMLs → migrate.
    assert _should_migrate_preexisting(UserPreferences(), has_device_configs=True)
    # Unchosen, never onboarded, no configs → fresh install, no migration.
    assert not _should_migrate_preexisting(UserPreferences(), has_device_configs=False)


def test_mark_preexisting_acknowledges_only_a_completed_install() -> None:
    """The marker sets YAML always, but acknowledges only a completed install."""
    completed = UserPreferences(onboarding_completed_version=1)
    _mark_preexisting(completed)
    assert completed.experience_level == ExperienceLevel.EXPERT
    assert completed.onboarding_completed_version == ONBOARDING_VERSION

    config_only = UserPreferences()
    _mark_preexisting(config_only)
    assert config_only.experience_level == ExperienceLevel.EXPERT
    assert config_only.onboarding_completed_version == 0


def test_has_device_configs_missing_dir_returns_false(tmp_path: Path) -> None:
    """A genuinely-absent config dir is a fresh install, not a scan failure."""
    assert _has_device_configs(tmp_path / "does-not-exist") is False


def test_has_device_configs_unreadable_dir_assumes_preexisting(tmp_path: Path) -> None:
    """A dir that exists but can't be read fails safe for existing users.

    A transient read error must not reclassify a real install as fresh and
    re-pop the wizard, so it assumes configs are present.
    """
    with patch(
        "esphome_device_builder.controllers.onboarding.list_yaml_files",
        side_effect=PermissionError("denied"),
    ):
        assert _has_device_configs(tmp_path) is True


async def test_migrate_fresh_install_is_noop(tmp_path: Path) -> None:
    """No prior onboarding and no device YAML ⇒ stay unchosen, see the wizard."""
    controller = _make_controller(tmp_path)
    await controller.migrate_preexisting_install()
    prefs = controller._prefs.snapshot()
    assert prefs.experience_level is None
    assert prefs.onboarding_completed_version == 0


async def test_migrate_ignores_secrets_yaml(tmp_path: Path) -> None:
    """``secrets.yaml`` alone is not a device config — no migration."""
    _write_secrets(tmp_path, "wifi_ssid: home\n")
    controller = _make_controller(tmp_path)
    await controller.migrate_preexisting_install()
    assert controller._prefs.snapshot().experience_level is None


async def test_migrate_preserves_an_explicit_choice(tmp_path: Path) -> None:
    """A user who already picked BEGINNER isn't overwritten by migration."""
    (tmp_path / "device.yaml").write_text("esphome:\n  name: device\n")
    controller = _make_controller(
        tmp_path,
        prefs=UserPreferences(
            experience_level=ExperienceLevel.BEGINNER, onboarding_completed_version=2
        ),
    )
    await controller.migrate_preexisting_install()
    assert controller._prefs.snapshot().experience_level == ExperienceLevel.BEGINNER


# ---------------------------------------------------------------------------
# Constructor smoke
# ---------------------------------------------------------------------------


def test_constructor_stores_db_reference() -> None:
    db = MagicMock()
    controller = OnboardingController(db)
    assert controller._db is db
