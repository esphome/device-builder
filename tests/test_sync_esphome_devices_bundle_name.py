"""``_bundle_name_for`` falls back off the ESPHome ``name: None`` sentinel."""

from __future__ import annotations

from typing import Any

from script.sync_esphome_devices import (  # type: ignore[import-not-found]
    _bundle_name_for,
    _Candidate,
)


def _cand(item: dict[str, Any], platform: str = "rgbww") -> _Candidate:
    return _Candidate(
        item=item,
        domain="light",
        platform=platform,
        component_id=f"light.{platform}",
        component={},
        local_id="x",
        fields={},
        counter=1,
    )


def test_name_none_sentinel_falls_back_to_platform() -> None:
    """``name: None`` (use device name) must not read as a literal "None (full setup)"."""
    assert _bundle_name_for(_cand({"name": "None"})) == "Rgbww (full setup)"
    assert _bundle_name_for(_cand({"name": "none"})) == "Rgbww (full setup)"


def test_real_name_is_used() -> None:
    assert _bundle_name_for(_cand({"name": "Desk Lamp"})) == "Desk Lamp (full setup)"
