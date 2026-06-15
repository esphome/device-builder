"""Tests for ``OnboardingController`` — the dashboard onboarding flow.

Covers ``get_state``, ``set_wifi_credentials``, and
``mark_acknowledged`` against a per-test ``tmp_path`` config dir.
The controller is constructed via ``__new__`` so we can stub
``self._db.settings`` without driving the full ``DeviceBuilder``
init chain (mirrors the pattern from ``test_config_controller``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from esphome_device_builder.controllers.config._preferences_store import PreferencesStore
from esphome_device_builder.controllers.onboarding import (
    OnboardingController,
    _has_device_configs,
    _mark_preexisting,
    _should_migrate_preexisting,
)
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.secrets_state import (
    PLACEHOLDER_WIFI_PASSWORD,
    PLACEHOLDER_WIFI_SSID,
    _replace_or_append_secret,
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
# get_state
# ---------------------------------------------------------------------------


async def test_get_state_pending_for_missing_secrets(tmp_path: Path) -> None:
    """No ``secrets.yaml`` ⇒ wifi step pending, version baseline."""
    controller = _make_controller(tmp_path)
    state = await controller.get_state()
    assert state.current_version == ONBOARDING_VERSION
    assert state.completed_version == 0
    assert _step(state, OnboardingStepId.WIFI_CREDENTIALS) == OnboardingStepStatus.PENDING


async def test_get_state_pending_for_empty_string_secrets(tmp_path: Path) -> None:
    """Existing-install bootstrap with ``wifi_ssid: ""`` ⇒ still pending."""
    _write_secrets(tmp_path, 'wifi_ssid: ""\nwifi_password: ""\n')
    controller = _make_controller(tmp_path)
    state = await controller.get_state()
    assert _step(state, OnboardingStepId.WIFI_CREDENTIALS) == OnboardingStepStatus.PENDING


async def test_get_state_pending_for_placeholder_secrets(tmp_path: Path) -> None:
    """Fresh-install bootstrap with the placeholder ⇒ still pending."""
    _write_secrets(
        tmp_path,
        f'wifi_ssid: "{PLACEHOLDER_WIFI_SSID}"\nwifi_password: "{PLACEHOLDER_WIFI_PASSWORD}"\n',
    )
    controller = _make_controller(tmp_path)
    state = await controller.get_state()
    assert _step(state, OnboardingStepId.WIFI_CREDENTIALS) == OnboardingStepStatus.PENDING


async def test_get_state_done_for_real_secrets(tmp_path: Path) -> None:
    _write_secrets(tmp_path, "wifi_ssid: home_network\nwifi_password: hunter2\n")
    controller = _make_controller(tmp_path)
    state = await controller.get_state()
    assert _step(state, OnboardingStepId.WIFI_CREDENTIALS) == OnboardingStepStatus.DONE


# ---------------------------------------------------------------------------
# get_state — environment- and preference-aware step list
# ---------------------------------------------------------------------------


async def test_get_state_non_ha_includes_use_case_step(tmp_path: Path) -> None:
    """Non-HA installs ask the remote-compute use-case question."""
    controller = _make_controller(tmp_path, on_ha_addon=False)
    state = await controller.get_state()
    ids = [s.id for s in state.steps]
    assert ids == [
        OnboardingStepId.USE_CASE,
        OnboardingStepId.EXPERIENCE_LEVEL,
        OnboardingStepId.WIFI_CREDENTIALS,
    ]
    assert _step(state, OnboardingStepId.USE_CASE) == OnboardingStepStatus.PENDING
    assert _step(state, OnboardingStepId.EXPERIENCE_LEVEL) == OnboardingStepStatus.PENDING


async def test_get_state_ha_addon_omits_use_case_step(tmp_path: Path) -> None:
    """HA addon manages devices in HA, so no use-case question."""
    controller = _make_controller(tmp_path, on_ha_addon=True)
    state = await controller.get_state()
    ids = [s.id for s in state.steps]
    assert ids == [OnboardingStepId.EXPERIENCE_LEVEL, OnboardingStepId.WIFI_CREDENTIALS]


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


async def test_get_state_remote_compute_only_drops_wifi_step(tmp_path: Path) -> None:
    """A remote-compute-only install skips Wi-Fi setup entirely."""
    controller = _make_controller(
        tmp_path, on_ha_addon=False, prefs=UserPreferences(remote_compute_only=True)
    )
    state = await controller.get_state()
    assert _step(state, OnboardingStepId.WIFI_CREDENTIALS) is None
    assert OnboardingStepId.USE_CASE in [s.id for s in state.steps]


# ---------------------------------------------------------------------------
# set_wifi_credentials — happy path + validation
# ---------------------------------------------------------------------------


async def test_set_wifi_credentials_writes_to_secrets_yaml(tmp_path: Path) -> None:
    """The setter updates the file and the next get_state reflects it."""
    _write_secrets(
        tmp_path,
        f'wifi_ssid: "{PLACEHOLDER_WIFI_SSID}"\nwifi_password: "{PLACEHOLDER_WIFI_PASSWORD}"\n',
    )
    controller = _make_controller(tmp_path)
    state = await controller.set_wifi_credentials(ssid="home_network", password="hunter2")
    assert _step(state, OnboardingStepId.WIFI_CREDENTIALS) == OnboardingStepStatus.DONE
    content = (tmp_path / "secrets.yaml").read_text()
    assert 'wifi_ssid: "home_network"' in content
    assert 'wifi_password: "hunter2"' in content


async def test_set_wifi_credentials_preserves_other_secrets_and_comments(
    tmp_path: Path,
) -> None:
    """Line-based update keeps unrelated keys + comments untouched."""
    _write_secrets(
        tmp_path,
        "# my secrets file\n"
        "api_key: ABC123\n"
        f'wifi_ssid: "{PLACEHOLDER_WIFI_SSID}"\n'
        "# wifi password follows\n"
        f'wifi_password: "{PLACEHOLDER_WIFI_PASSWORD}"\n'
        "mqtt_broker: 10.0.0.1\n",
    )
    controller = _make_controller(tmp_path)
    await controller.set_wifi_credentials(ssid="MyAP", password="secret")
    content = (tmp_path / "secrets.yaml").read_text()
    assert "# my secrets file" in content
    assert "api_key: ABC123" in content
    assert "# wifi password follows" in content
    assert "mqtt_broker: 10.0.0.1" in content
    assert 'wifi_ssid: "MyAP"' in content
    assert 'wifi_password: "secret"' in content


async def test_set_wifi_credentials_creates_file_when_missing(tmp_path: Path) -> None:
    """User who deleted secrets.yaml between bootstrap and onboarding."""
    controller = _make_controller(tmp_path)
    await controller.set_wifi_credentials(ssid="MyAP", password="secret")
    content = (tmp_path / "secrets.yaml").read_text()
    assert 'wifi_ssid: "MyAP"' in content
    assert 'wifi_password: "secret"' in content


async def test_set_wifi_credentials_preserves_ssid_whitespace(tmp_path: Path) -> None:
    """IEEE 802.11 allows leading/trailing whitespace in SSIDs.

    Trimming would silently change the network name and the device
    would fail to associate. Preserve the value as-typed; the
    user knows what their AP advertises.
    """
    controller = _make_controller(tmp_path)
    await controller.set_wifi_credentials(ssid="  MyNetwork  ", password="hunter2")
    content = (tmp_path / "secrets.yaml").read_text()
    assert 'wifi_ssid: "  MyNetwork  "' in content


async def test_set_wifi_credentials_quotes_double_quotes_safely(
    tmp_path: Path,
) -> None:
    """SSIDs with ``"`` need escaping inside the double-quoted scalar."""
    controller = _make_controller(tmp_path)
    await controller.set_wifi_credentials(ssid='Net"With"Quotes', password="p")
    content = (tmp_path / "secrets.yaml").read_text()
    assert r'wifi_ssid: "Net\"With\"Quotes"' in content


async def test_set_wifi_credentials_rejects_empty_ssid(tmp_path: Path) -> None:
    controller = _make_controller(tmp_path)
    with pytest.raises(CommandError, match="SSID can't be empty"):
        await controller.set_wifi_credentials(ssid="   ", password="p")


async def test_set_wifi_credentials_rejects_non_string_ssid(tmp_path: Path) -> None:
    """A misbehaving client sending a number / null gets a clean error.

    The WS layer doesn't enforce JSON value types, so without the
    isinstance gate ``ssid: 42`` would reach ``.strip()`` and
    surface as ``INTERNAL_ERROR``.
    """
    controller = _make_controller(tmp_path)
    with pytest.raises(CommandError, match="SSID must be a string"):
        await controller.set_wifi_credentials(ssid=42, password="p")  # type: ignore[arg-type]


async def test_set_wifi_credentials_rejects_non_string_password(tmp_path: Path) -> None:
    controller = _make_controller(tmp_path)
    with pytest.raises(CommandError, match="Password must be a string"):
        await controller.set_wifi_credentials(ssid="MyAP", password=None)  # type: ignore[arg-type]


async def test_set_wifi_credentials_rejects_oversize_ssid(tmp_path: Path) -> None:
    controller = _make_controller(tmp_path)
    with pytest.raises(CommandError, match="32 characters"):
        await controller.set_wifi_credentials(ssid="A" * 33, password="p")


async def test_set_wifi_credentials_rejects_oversize_password(
    tmp_path: Path,
) -> None:
    controller = _make_controller(tmp_path)
    with pytest.raises(CommandError, match="64 characters"):
        await controller.set_wifi_credentials(ssid="MyAP", password="P" * 65)


async def test_set_wifi_credentials_accepts_empty_password(tmp_path: Path) -> None:
    """Open networks have empty passwords — must not be rejected."""
    controller = _make_controller(tmp_path)
    state = await controller.set_wifi_credentials(ssid="OpenNet", password="")
    assert _step(state, OnboardingStepId.WIFI_CREDENTIALS) == OnboardingStepStatus.DONE


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
# Newline / control-char rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ssid",
    [
        "My\nNetwork",
        "My\rNetwork",
        "My\x00Network",
        "My\x07Network",  # BEL — would silently break PyYAML round-trip
        "My\x1bNetwork",  # ESC
        "My\x7fNetwork",  # DEL
    ],
)
async def test_set_wifi_credentials_rejects_newlines_in_ssid(tmp_path: Path, ssid: str) -> None:
    r"""Reject newline / NUL injection in the SSID input.

    A ``\n`` in the SSID would inject extra YAML lines via the
    line-based rewrite; a ``\0`` would terminate the file early
    on read. Block up-front so the next save can't break
    ``secrets.yaml``.
    """
    controller = _make_controller(tmp_path)
    with pytest.raises(CommandError, match="control character"):
        await controller.set_wifi_credentials(ssid=ssid, password="p")


async def test_set_wifi_credentials_rejects_newlines_in_password(
    tmp_path: Path,
) -> None:
    controller = _make_controller(tmp_path)
    with pytest.raises(CommandError, match="control character"):
        await controller.set_wifi_credentials(ssid="MyAP", password="p\nass")


async def test_set_wifi_credentials_allows_tab_in_value(tmp_path: Path) -> None:
    """Allow TAB through — don't over-block.

    TAB is the one control character ESPHome's
    ``cv.string_strict`` accepts.
    """
    controller = _make_controller(tmp_path)
    state = await controller.set_wifi_credentials(ssid="MyAP", password="hunter\t2")
    assert _step(state, OnboardingStepId.WIFI_CREDENTIALS) == OnboardingStepStatus.DONE


async def test_set_wifi_credentials_preserves_inline_comments(
    tmp_path: Path,
) -> None:
    """A power-user `wifi_ssid: foo  # office` keeps the annotation.

    The line-based rewrite captures the trailing ``  # …`` and
    re-attaches it after replacing the value. Without this, the
    old behaviour stripped any inline annotation on credential
    lines.
    """
    _write_secrets(
        tmp_path,
        'wifi_ssid: "old"  # Apt 4B router\nwifi_password: "p"  # WPA2\n',
    )
    controller = _make_controller(tmp_path)
    await controller.set_wifi_credentials(ssid="MyAP", password="newpw")
    content = (tmp_path / "secrets.yaml").read_text()
    assert 'wifi_ssid: "MyAP"  # Apt 4B router' in content
    assert 'wifi_password: "newpw"  # WPA2' in content


async def test_set_wifi_credentials_rewrites_duplicate_keys(
    tmp_path: Path,
) -> None:
    """Malformed `secrets.yaml` with the same key twice ⇒ rewrite both.

    Whether the resulting file then re-parses cleanly depends on
    the YAML loader's duplicate-key handling (PyYAML's default
    rejects duplicates outright, ruamel takes the last). What we
    can guarantee here is that the rewrite touches **every**
    occurrence of the key — leaving a stale duplicate behind
    would mean the new value never wins on the readers that *do*
    accept duplicates.
    """
    _write_secrets(
        tmp_path,
        'wifi_ssid: "old1"\nwifi_password: "p"\nwifi_ssid: "old2"\n',
    )
    controller = _make_controller(tmp_path)
    await controller.set_wifi_credentials(ssid="MyAP", password="p")
    content = (tmp_path / "secrets.yaml").read_text()
    # Both lines were overwritten — no stale ``wifi_ssid: "old…"``
    # left behind to override the new value on a reader that
    # silently picks the last occurrence.
    assert "old1" not in content
    assert "old2" not in content
    assert content.count('wifi_ssid: "MyAP"') == 2


# ---------------------------------------------------------------------------
# get_state — malformed secrets file fallback
# ---------------------------------------------------------------------------


async def test_get_state_pending_for_malformed_secrets_yaml(tmp_path: Path) -> None:
    """Treat malformed YAML as ``unconfigured`` instead of crashing.

    Falls back so the user can run the wizard to rewrite the file
    cleanly instead of being stuck with a broken state.
    """
    _write_secrets(tmp_path, "wifi_ssid: [unclosed\n")
    controller = _make_controller(tmp_path)
    state = await controller.get_state()
    assert _step(state, OnboardingStepId.WIFI_CREDENTIALS) == OnboardingStepStatus.PENDING


# ---------------------------------------------------------------------------
# _replace_or_append_secret — direct unit tests
# ---------------------------------------------------------------------------
#
# The helper is exercised end-to-end through ``set_wifi_credentials``
# above, but the regex it leans on is fiddly enough that isolated
# coverage is warranted. Anyone refactoring ``_SECRET_LINE_RE`` should
# see these break first.


def test_replace_or_append_secret_appends_when_key_absent_in_existing_file() -> None:
    """File exists with other keys — new key gets appended, not inlined."""
    result = _replace_or_append_secret("api_key: ABC\n", "wifi_ssid", "MyAP")
    assert result == 'api_key: ABC\nwifi_ssid: "MyAP"\n'


def test_replace_or_append_secret_appends_to_file_without_trailing_newline() -> None:
    """No trailing newline on input — helper adds one before appending."""
    result = _replace_or_append_secret("api_key: ABC", "wifi_ssid", "MyAP")
    assert result == 'api_key: ABC\nwifi_ssid: "MyAP"\n'


def test_replace_or_append_secret_appends_to_empty_content() -> None:
    """Empty input behaves like the missing-file path."""
    assert _replace_or_append_secret("", "wifi_ssid", "MyAP") == 'wifi_ssid: "MyAP"\n'


def test_replace_or_append_secret_preserves_indent() -> None:
    """Indented secret lines keep their indent on rewrite.

    ``secrets.yaml`` is conventionally flat, but a user that nested
    keys under a YAML anchor or parent shouldn't have the indent
    stripped — it would silently change the parsed structure.
    """
    result = _replace_or_append_secret('  wifi_ssid: "old"\n', "wifi_ssid", "new")
    assert result == '  wifi_ssid: "new"\n'


def test_replace_or_append_secret_quotes_special_characters() -> None:
    """Backslash and double-quote in the value get escaped, others pass through."""
    result = _replace_or_append_secret('wifi_password: "old"\n', "wifi_password", 'p\\a"s s')
    assert result == 'wifi_password: "p\\\\a\\"s s"\n'


def test_replace_or_append_secret_only_matches_full_key_name() -> None:
    r"""``wifi_ssid_backup`` is not the same key as ``wifi_ssid``.

    Without anchored matching, a substring match would clobber an
    unrelated key. The regex ``\w+`` greedily eats the whole
    identifier, but a future refactor that switches to ``startswith``
    or ``in`` would silently break this — pin it down.
    """
    result = _replace_or_append_secret('wifi_ssid_backup: "keep"\n', "wifi_ssid", "MyAP")
    # ``wifi_ssid_backup`` line untouched, new key appended.
    assert 'wifi_ssid_backup: "keep"' in result
    assert 'wifi_ssid: "MyAP"' in result


def test_replace_or_append_secret_ignores_pure_comment_lines() -> None:
    """A standalone ``# wifi_ssid: foo`` comment is not a key.

    Edge case: a user may have a commented-out example. The regex
    starts with ``[a-zA-Z_]`` so ``#`` lines never match — the new
    key is appended below.
    """
    result = _replace_or_append_secret(
        '# wifi_ssid: "example"\napi_key: ABC\n', "wifi_ssid", "MyAP"
    )
    assert '# wifi_ssid: "example"' in result
    assert 'wifi_ssid: "MyAP"' in result


def test_replace_or_append_secret_preserves_inline_comment_with_special_chars() -> None:
    """Trailing ``# comment with : colons`` round-trips intact."""
    result = _replace_or_append_secret(
        'wifi_ssid: "old"  # see ticket: ABC-123\n', "wifi_ssid", "MyAP"
    )
    assert result == 'wifi_ssid: "MyAP"  # see ticket: ABC-123\n'


def test_replace_or_append_secret_handles_bare_key() -> None:
    """``wifi_ssid:`` with no value still matches and gets the new value."""
    result = _replace_or_append_secret("wifi_ssid:\n", "wifi_ssid", "MyAP")
    assert result == 'wifi_ssid: "MyAP"\n'


def test_replace_or_append_secret_value_with_hash_in_quotes_is_misparsed() -> None:
    """Known limitation: ``# `` inside a quoted value confuses the regex.

    The line regex treats `` # `` (space-then-hash) anywhere on the
    line as a trailing comment, so a previous value containing
    ``"foo # bar"`` gets split — the new value lands but a bogus
    `` # bar"`` is appended as a "comment". The result is still
    valid YAML (the `` #`` truly becomes a comment on the rewrite),
    but the original spurious tail is preserved verbatim.

    This test pins the behaviour so a future regex tightening that
    *does* fix this case has a green-then-red breadcrumb. Realistic
    impact: low — a power user with ``#`` in their SSID who edits
    the file by hand and then runs the wizard.
    """
    result = _replace_or_append_secret('wifi_ssid: "foo # bar"\n', "wifi_ssid", "MyAP")
    assert result == 'wifi_ssid: "MyAP" # bar"\n'


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
