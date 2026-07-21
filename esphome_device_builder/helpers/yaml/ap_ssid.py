"""Fallback-AP SSID derivation and the identity-following rewrite."""

from __future__ import annotations

from .scalar import (
    _safe_yaml_scalar,
    _strip_yaml_quotes,
    is_plain_literal_scalar,
    rewrite_yaml_scalar,
)

# ESPHome's ``cv.ssid`` caps an AP ssid at 32 bytes.
_AP_SSID_MAX_LEN = 32

_WIFI_AP_SSID_PATH = ("wifi", "ap", "ssid")


def fallback_ap_ssid(label: str) -> str:
    """AP ssid ``<label> Fallback Hotspot``; trims <label> so the marker survives the cap."""
    base = label.strip() or "ESPHome"
    suffix = " Fallback Hotspot"
    # Trim the name, not the marker (esphome's wizard drops the whole
    # marker here), so the recovery AP stays identifiable for long names.
    if len(base) + len(suffix) > _AP_SSID_MAX_LEN:
        base = base[: _AP_SSID_MAX_LEN - len(suffix)]
    return f"{base}{suffix}"


def rewrite_fallback_ap_ssid(yaml_text: str, old_label: str | None, new_label: str | None) -> str:
    """
    Retarget a generator-shaped ``wifi.ap.ssid`` from *old_label* to *new_label*.

    Rewrites only when the current value is exactly the fallback SSID
    :func:`fallback_ap_ssid` derives from *old_label*; a customized SSID
    or a ``!secret`` / ``${...}`` indirection stays untouched.
    """
    if not old_label or not new_label or old_label == new_label:
        return yaml_text

    def _swap(raw: str) -> str | None:
        if not is_plain_literal_scalar(raw):
            return None
        if _strip_yaml_quotes(raw) != fallback_ap_ssid(old_label):
            return None
        return _safe_yaml_scalar(fallback_ap_ssid(new_label))

    return rewrite_yaml_scalar(yaml_text, _WIFI_AP_SSID_PATH, _swap)
