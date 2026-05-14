"""
Detection helpers for placeholder / empty secrets values.

The dashboard's first-run bootstrap writes deterministic placeholder
strings into ``secrets.yaml`` so ``!secret wifi_ssid`` references in
generated YAML resolve cleanly through ESPHome's validator. The
onboarding controller uses the same constants here to detect whether
the user has supplied real values yet — keeping the bootstrap and
the state-check anchored to one source of truth so a future change
to the placeholder text doesn't desync the two.
"""

from __future__ import annotations

from pathlib import Path

from esphome import yaml_util
from esphome.core import EsphomeError

# Bootstrap placeholder strings — hoisted from
# ``controllers/config.py`` so the writer and the reader share a
# single definition.
PLACEHOLDER_WIFI_SSID = "REPLACE_WITH_YOUR_WIFI_NETWORK"
PLACEHOLDER_WIFI_PASSWORD = "REPLACE_WITH_YOUR_WIFI_PASSWORD"  # noqa: S105 — obvious placeholder, not a real credential

# Values that count as "not user-configured" for ``wifi_ssid``:
# missing key, empty string, or the bootstrap placeholder. Stored
# as a frozenset so a future placeholder rotation just appends
# the old value here for backward compatibility.
_UNCONFIGURED_WIFI_SSID_VALUES: frozenset[str] = frozenset({"", PLACEHOLDER_WIFI_SSID})


def read_secrets_yaml(config_dir: Path) -> dict | None:
    """
    Load ``secrets.yaml`` as a plain dict, or ``None`` on any failure.

    Centralised so every reader (``ConfigController.get_secrets``,
    ``OnboardingController.get_state``, future MQTT-broker pickup
    etc.) shares one fail-soft contract: missing file ⇒ ``None``,
    parse error ⇒ ``None``, non-dict top-level (``secrets.yaml``
    that's a list or scalar — invalid but possible) ⇒ ``None``.

    ``yaml_util.load_yaml`` expects a ``Path``, not a ``str`` — the
    type signature pins this so a string slip from a caller fails
    at type-check time instead of as an ``AttributeError`` slipping
    past the narrow ``EsphomeError`` catch below.
    """
    secrets_path = config_dir / "secrets.yaml"
    if not secrets_path.exists():
        return None
    try:
        data = yaml_util.load_yaml(secrets_path)
    except EsphomeError:
        return None
    return data if isinstance(data, dict) else None


def is_wifi_unconfigured(secrets: dict | None) -> bool:
    """
    Return True when ``secrets.yaml``'s ``wifi_ssid`` is missing / empty / placeholder.

    Only the SSID is checked — ESPHome's ``cv.ssid`` validator
    rejects empty strings ("SSID can't be empty.") while
    ``cv.string_strict`` on the password accepts ``""`` (open
    networks are valid). So the SSID is the canonical "wifi
    is configured" signal; matching on it alone keeps the
    state-check minimal.

    Boundary cases:

    - Missing file / empty dict / missing key → unconfigured.
    - Non-string value (e.g. ``wifi_ssid: 42`` — quotes stripped
      by accident) → unconfigured. ESPHome's compile-time
      validator would reject it later anyway, and clearing
      onboarding here would mask a real broken-config state from
      the user.
    """
    if not secrets:
        return True
    val = secrets.get("wifi_ssid")
    if not isinstance(val, str):
        return True
    return val.strip() in _UNCONFIGURED_WIFI_SSID_VALUES
