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


def is_wifi_unconfigured(secrets: dict | None) -> bool:
    """
    Return True when ``secrets.yaml``'s ``wifi_ssid`` is missing / empty / placeholder.

    Only the SSID is checked — ESPHome's ``cv.ssid`` validator
    rejects empty strings ("SSID can't be empty.") while
    ``cv.string_strict`` on the password accepts ``""`` (open
    networks are valid). So the SSID is the canonical "wifi
    is configured" signal; matching on it alone keeps the
    state-check minimal.

    A ``None`` secrets dict (file missing) and a non-string
    ``wifi_ssid`` value (e.g. a typo'd ``wifi_ssid: 42`` —
    something the user has clearly tried to set) are handled at
    the boundaries: missing → unconfigured (need to prompt),
    non-string → configured (we won't second-guess what the user
    typed).
    """
    if not secrets:
        return True
    val = secrets.get("wifi_ssid")
    if val is None:
        return True
    if not isinstance(val, str):
        return False
    return val.strip() in _UNCONFIGURED_WIFI_SSID_VALUES
