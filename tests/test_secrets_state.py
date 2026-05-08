"""Tests for ``helpers.secrets_state.is_wifi_unconfigured``.

Covers every call shape the onboarding controller can hand it:
missing file (``None``), empty dict, missing ``wifi_ssid`` key,
empty-string value, the bootstrap placeholder, a real value, and
a non-string typo.
"""

from __future__ import annotations

import pytest

from esphome_device_builder.helpers.secrets_state import (
    PLACEHOLDER_WIFI_PASSWORD,
    PLACEHOLDER_WIFI_SSID,
    is_wifi_unconfigured,
)


def test_unconfigured_when_secrets_is_none() -> None:
    """File missing entirely ⇒ user needs to set credentials."""
    assert is_wifi_unconfigured(None) is True


def test_unconfigured_when_secrets_is_empty_dict() -> None:
    """File present but empty ⇒ same as missing for our purposes."""
    assert is_wifi_unconfigured({}) is True


def test_unconfigured_when_wifi_ssid_key_is_missing() -> None:
    """Other secrets present but no ``wifi_ssid`` ⇒ unconfigured."""
    assert is_wifi_unconfigured({"api_key": "ZZZ", "mqtt_pw": "shhh"}) is True


def test_unconfigured_when_wifi_ssid_is_empty_string() -> None:
    """Existing installs from the previous bootstrap ⇒ still unconfigured."""
    assert is_wifi_unconfigured({"wifi_ssid": ""}) is True


def test_unconfigured_when_wifi_ssid_is_only_whitespace() -> None:
    """``"  "`` should be treated like empty — strip before comparing."""
    assert is_wifi_unconfigured({"wifi_ssid": "   "}) is True


def test_unconfigured_when_wifi_ssid_matches_bootstrap_placeholder() -> None:
    """Fresh-install placeholder ⇒ user hasn't replaced it yet."""
    assert is_wifi_unconfigured({"wifi_ssid": PLACEHOLDER_WIFI_SSID}) is True


def test_configured_when_wifi_ssid_is_a_real_value() -> None:
    assert is_wifi_unconfigured({"wifi_ssid": "home_network"}) is False


def test_configured_when_wifi_ssid_is_a_non_string_typo() -> None:
    """``wifi_ssid: 42`` — user clearly typed something. Don't second-guess."""
    assert is_wifi_unconfigured({"wifi_ssid": 42}) is False


@pytest.mark.parametrize(
    "value",
    ["MyNetwork", "REPLACE_WITH_OTHER_THING", "  spaced  network  "],
)
def test_password_does_not_affect_configured_state(value: str) -> None:
    """Password value is intentionally not part of the check.

    Open networks legitimately have an empty password.
    """
    assert is_wifi_unconfigured({"wifi_ssid": value, "wifi_password": ""}) is False


def test_placeholder_password_constant_is_exported() -> None:
    """Pin the placeholder password export.

    The constant is unused by ``is_wifi_unconfigured`` but
    exported alongside the SSID one because the bootstrap and
    the onboarding setter both need it. Locking the export here
    prevents a future refactor from silently moving it.
    """
    assert isinstance(PLACEHOLDER_WIFI_PASSWORD, str)
    assert PLACEHOLDER_WIFI_PASSWORD
