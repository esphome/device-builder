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
from unittest.mock import MagicMock

import pytest

from esphome_device_builder.controllers.config import save_preferences
from esphome_device_builder.controllers.onboarding import OnboardingController
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.secrets_state import (
    PLACEHOLDER_WIFI_PASSWORD,
    PLACEHOLDER_WIFI_SSID,
)
from esphome_device_builder.models.onboarding import (
    ONBOARDING_VERSION,
    OnboardingStepId,
    OnboardingStepStatus,
)
from esphome_device_builder.models.preferences import UserPreferences


def _make_controller(config_dir: Path) -> OnboardingController:
    controller = OnboardingController.__new__(OnboardingController)
    controller._db = MagicMock()
    controller._db.settings.config_dir = config_dir
    controller._db.settings.absolute_config_dir = config_dir.resolve()
    return controller


def _write_secrets(config_dir: Path, content: str) -> None:
    (config_dir / "secrets.yaml").write_text(content)


# ---------------------------------------------------------------------------
# get_state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_state_pending_for_missing_secrets(tmp_path: Path) -> None:
    """No ``secrets.yaml`` ⇒ wifi step pending, version baseline."""
    controller = _make_controller(tmp_path)
    state = await controller.get_state()
    assert state.current_version == ONBOARDING_VERSION
    assert state.completed_version == 0
    assert len(state.steps) == 1
    assert state.steps[0].id == OnboardingStepId.WIFI_CREDENTIALS
    assert state.steps[0].status == OnboardingStepStatus.PENDING


@pytest.mark.asyncio
async def test_get_state_pending_for_empty_string_secrets(tmp_path: Path) -> None:
    """Existing-install bootstrap with ``wifi_ssid: ""`` ⇒ still pending."""
    _write_secrets(tmp_path, 'wifi_ssid: ""\nwifi_password: ""\n')
    controller = _make_controller(tmp_path)
    state = await controller.get_state()
    assert state.steps[0].status == OnboardingStepStatus.PENDING


@pytest.mark.asyncio
async def test_get_state_pending_for_placeholder_secrets(tmp_path: Path) -> None:
    """Fresh-install bootstrap with the placeholder ⇒ still pending."""
    _write_secrets(
        tmp_path,
        f'wifi_ssid: "{PLACEHOLDER_WIFI_SSID}"\nwifi_password: "{PLACEHOLDER_WIFI_PASSWORD}"\n',
    )
    controller = _make_controller(tmp_path)
    state = await controller.get_state()
    assert state.steps[0].status == OnboardingStepStatus.PENDING


@pytest.mark.asyncio
async def test_get_state_done_for_real_secrets(tmp_path: Path) -> None:
    _write_secrets(tmp_path, "wifi_ssid: home_network\nwifi_password: hunter2\n")
    controller = _make_controller(tmp_path)
    state = await controller.get_state()
    assert state.steps[0].status == OnboardingStepStatus.DONE


# ---------------------------------------------------------------------------
# set_wifi_credentials — happy path + validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_wifi_credentials_writes_to_secrets_yaml(tmp_path: Path) -> None:
    """The setter updates the file and the next get_state reflects it."""
    _write_secrets(
        tmp_path,
        f'wifi_ssid: "{PLACEHOLDER_WIFI_SSID}"\nwifi_password: "{PLACEHOLDER_WIFI_PASSWORD}"\n',
    )
    controller = _make_controller(tmp_path)
    state = await controller.set_wifi_credentials(ssid="home_network", password="hunter2")
    assert state.steps[0].status == OnboardingStepStatus.DONE
    content = (tmp_path / "secrets.yaml").read_text()
    assert 'wifi_ssid: "home_network"' in content
    assert 'wifi_password: "hunter2"' in content


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_set_wifi_credentials_creates_file_when_missing(tmp_path: Path) -> None:
    """User who deleted secrets.yaml between bootstrap and onboarding."""
    controller = _make_controller(tmp_path)
    await controller.set_wifi_credentials(ssid="MyAP", password="secret")
    content = (tmp_path / "secrets.yaml").read_text()
    assert 'wifi_ssid: "MyAP"' in content
    assert 'wifi_password: "secret"' in content


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_set_wifi_credentials_quotes_double_quotes_safely(
    tmp_path: Path,
) -> None:
    """SSIDs with ``"`` need escaping inside the double-quoted scalar."""
    controller = _make_controller(tmp_path)
    await controller.set_wifi_credentials(ssid='Net"With"Quotes', password="p")
    content = (tmp_path / "secrets.yaml").read_text()
    assert r'wifi_ssid: "Net\"With\"Quotes"' in content


@pytest.mark.asyncio
async def test_set_wifi_credentials_rejects_empty_ssid(tmp_path: Path) -> None:
    controller = _make_controller(tmp_path)
    with pytest.raises(CommandError, match="SSID can't be empty"):
        await controller.set_wifi_credentials(ssid="   ", password="p")


@pytest.mark.asyncio
async def test_set_wifi_credentials_rejects_oversize_ssid(tmp_path: Path) -> None:
    controller = _make_controller(tmp_path)
    with pytest.raises(CommandError, match="32 characters"):
        await controller.set_wifi_credentials(ssid="A" * 33, password="p")


@pytest.mark.asyncio
async def test_set_wifi_credentials_rejects_oversize_password(
    tmp_path: Path,
) -> None:
    controller = _make_controller(tmp_path)
    with pytest.raises(CommandError, match="64 characters"):
        await controller.set_wifi_credentials(ssid="MyAP", password="P" * 65)


@pytest.mark.asyncio
async def test_set_wifi_credentials_accepts_empty_password(tmp_path: Path) -> None:
    """Open networks have empty passwords — must not be rejected."""
    controller = _make_controller(tmp_path)
    state = await controller.set_wifi_credentials(ssid="OpenNet", password="")
    assert state.steps[0].status == OnboardingStepStatus.DONE


# ---------------------------------------------------------------------------
# mark_acknowledged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_acknowledged_persists_current_version(tmp_path: Path) -> None:
    controller = _make_controller(tmp_path)
    state = await controller.mark_acknowledged()
    assert state.completed_version == ONBOARDING_VERSION
    # Re-read on a fresh controller to confirm the prefs file landed.
    state2 = await _make_controller(tmp_path).get_state()
    assert state2.completed_version == ONBOARDING_VERSION


@pytest.mark.asyncio
async def test_mark_acknowledged_is_idempotent(tmp_path: Path) -> None:
    controller = _make_controller(tmp_path)
    await controller.mark_acknowledged()
    state = await controller.mark_acknowledged()
    assert state.completed_version == ONBOARDING_VERSION


@pytest.mark.asyncio
async def test_mark_acknowledged_does_not_downgrade_a_higher_stored_version(
    tmp_path: Path,
) -> None:
    """Don't lose a future-build acknowledgement on rollback.

    A user who briefly ran a future build with
    ``ONBOARDING_VERSION = 2`` and then rolled back to this
    build (``= 1``) keeps the higher stored value — otherwise
    they'd be re-prompted on the next upgrade for steps they've
    already done.
    """
    future = UserPreferences(onboarding_completed_version=ONBOARDING_VERSION + 5)
    # ``save_preferences`` does sync filesystem I/O that ``blockbuster``
    # rejects when called inline from an async test. Hop to an executor
    # so we behave like the controller does in production.
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, save_preferences, tmp_path, future)
    controller = _make_controller(tmp_path)
    state = await controller.mark_acknowledged()
    assert state.completed_version == ONBOARDING_VERSION + 5


# ---------------------------------------------------------------------------
# Newline / control-char rejection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_set_wifi_credentials_rejects_newlines_in_password(
    tmp_path: Path,
) -> None:
    controller = _make_controller(tmp_path)
    with pytest.raises(CommandError, match="control character"):
        await controller.set_wifi_credentials(ssid="MyAP", password="p\nass")


@pytest.mark.asyncio
async def test_set_wifi_credentials_allows_tab_in_value(tmp_path: Path) -> None:
    """Allow TAB through — don't over-block.

    TAB is the one control character ESPHome's
    ``cv.string_strict`` accepts.
    """
    controller = _make_controller(tmp_path)
    state = await controller.set_wifi_credentials(ssid="MyAP", password="hunter\t2")
    assert state.steps[0].status == OnboardingStepStatus.DONE


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_get_state_pending_for_malformed_secrets_yaml(tmp_path: Path) -> None:
    """Treat malformed YAML as ``unconfigured`` instead of crashing.

    Falls back so the user can run the wizard to rewrite the file
    cleanly instead of being stuck with a broken state.
    """
    _write_secrets(tmp_path, "wifi_ssid: [unclosed\n")
    controller = _make_controller(tmp_path)
    state = await controller.get_state()
    assert state.steps[0].status == OnboardingStepStatus.PENDING


# ---------------------------------------------------------------------------
# Constructor smoke
# ---------------------------------------------------------------------------


def test_constructor_stores_db_reference() -> None:
    db = MagicMock()
    controller = OnboardingController(db)
    assert controller._db is db
