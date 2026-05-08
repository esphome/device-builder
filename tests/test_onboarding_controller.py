"""Tests for ``OnboardingController`` — the dashboard onboarding flow.

Covers ``get_state``, ``set_wifi_credentials``, and
``mark_acknowledged`` against a per-test ``tmp_path`` config dir.
The controller is constructed via ``__new__`` so we can stub
``self._db.settings`` without driving the full ``DeviceBuilder``
init chain (mirrors the pattern from ``test_config_controller``).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

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
async def test_set_wifi_credentials_strips_ssid_whitespace(tmp_path: Path) -> None:
    controller = _make_controller(tmp_path)
    await controller.set_wifi_credentials(ssid="  MyNetwork  ", password="hunter2")
    content = (tmp_path / "secrets.yaml").read_text()
    assert 'wifi_ssid: "MyNetwork"' in content


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
