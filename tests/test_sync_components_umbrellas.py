"""Unit tests for ``_inject_umbrella_entries`` in ``script/sync_components.py``.

The integration test in ``test_components_integration_docs.py`` exercises the
shipped component catalog — useful, but a regression in the sync logic
itself wouldn't fail CI as long as the file isn't regenerated. These tests
poke the function directly with synthetic catalog fragments so the contract
holds independent of whatever happens to be checked in.
"""

from __future__ import annotations

import pytest

from script.sync_components import (  # type: ignore[import-not-found]
    _UMBRELLA_ENTRIES,
    _inject_umbrella_entries,
)


def _platform_entry(component_id: str, *, image_url: str = "") -> dict:
    """Build a minimal catalog entry shaped like ``build_entries_from_file``'s output."""
    domain, _, _stem = component_id.partition(".")
    entry: dict = {
        "id": component_id,
        "name": component_id,
        "description": "",
        "category": domain,
    }
    if image_url:
        entry["image_url"] = image_url
    return entry


def test_injects_umbrella_for_each_configured_domain() -> None:
    """Every domain in ``_UMBRELLA_ENTRIES`` gets a synthesised top-level entry."""
    entries = [
        _platform_entry("ota.esphome", image_url="https://example/icon.svg"),
        _platform_entry("ota.http_request"),
        _platform_entry("time.homeassistant"),
        _platform_entry("time.sntp"),
        _platform_entry("sensor.dht"),  # unrelated, must be left alone
    ]

    _inject_umbrella_entries(entries)

    by_id = {e["id"]: e for e in entries}
    for spec in _UMBRELLA_ENTRIES:
        umbrella = by_id.get(spec["id"])
        assert umbrella is not None, f"missing umbrella for {spec['id']}"
        assert umbrella["category"] == spec["category"]
        # Description must name the implicit default platform — that's the
        # whole reason the umbrella exists.
        assert f"`{spec['default_platform']}`" in umbrella["description"]


def test_description_lists_every_present_platform() -> None:
    """The platform list in the description reflects what's in *entries* now.

    Re-derived at sync time so descriptions stay accurate when platforms are
    added or removed upstream — no hard-coded list to drift.
    """
    entries = [
        _platform_entry("ota.esphome"),
        _platform_entry("ota.http_request"),
        _platform_entry("ota.web_server"),
    ]

    _inject_umbrella_entries(entries)

    ota = next(e for e in entries if e["id"] == "ota")
    for stem in ("esphome", "http_request", "web_server"):
        assert f"`{stem}`" in ota["description"]
    # A platform we didn't include must NOT appear.
    assert "`zephyr_mcumgr`" not in ota["description"]


def test_borrows_image_url_from_default_platform() -> None:
    """Umbrella picks up the default platform's icon so the UI matches."""
    entries = [
        _platform_entry("ota.esphome", image_url="https://example/system-update.svg"),
        _platform_entry("ota.http_request", image_url="https://example/other.svg"),
    ]

    _inject_umbrella_entries(entries)

    ota = next(e for e in entries if e["id"] == "ota")
    assert ota["image_url"] == "https://example/system-update.svg"


def test_skips_when_default_platform_missing() -> None:
    """Defensive guard — without the default platform the umbrella would lie."""
    entries = [
        # ota.esphome is intentionally absent; only http_request is available.
        _platform_entry("ota.http_request"),
    ]

    _inject_umbrella_entries(entries)

    assert all(e["id"] != "ota" for e in entries), (
        "ota umbrella should not be added when the configured "
        "default platform `ota.esphome` is missing"
    )


def test_does_not_overwrite_existing_id() -> None:
    """If something else already owns the bare id, leave it alone."""
    pre_existing = {
        "id": "ota",
        "name": "Pre-existing",
        "description": "from elsewhere",
        "category": "ota",
    }
    entries = [
        _platform_entry("ota.esphome"),
        pre_existing,
    ]

    _inject_umbrella_entries(entries)

    ota_entries = [e for e in entries if e["id"] == "ota"]
    assert ota_entries == [pre_existing]


@pytest.mark.parametrize("spec", _UMBRELLA_ENTRIES)
def test_umbrella_spec_is_self_consistent(spec: dict[str, str]) -> None:
    """Each spec carries the keys ``_inject_umbrella_entries`` reads."""
    for required in ("id", "name", "category", "default_platform", "summary", "docs_url"):
        assert spec.get(required), f"{spec.get('id')!r} missing required field {required!r}"
    # ``default_platform`` is the bare stem — the function joins it as
    # ``f"{domain}.{default_platform}"`` to look up the platform entry.
    assert "." not in spec["default_platform"]
