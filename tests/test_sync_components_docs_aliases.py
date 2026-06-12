"""Unit tests for ``_shared_docs_page_aliases`` in ``script/sync_components.py``."""

from __future__ import annotations

import logging

import pytest

from script import sync_components
from script.sync_components import (  # type: ignore[import-not-found]
    _shared_docs_page_aliases,
)


@pytest.fixture
def fake_platforms(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Patch the target-platform set and per-platform ``auto_load`` results."""
    auto_loads: dict[str, list[str]] = {}
    monkeypatch.setattr(
        sync_components,
        "_TARGET_PLATFORMS",
        frozenset({"esp32", "bk72xx", "rtl87xx", "newchip"}),
    )
    monkeypatch.setattr(
        sync_components,
        "introspect_component",
        lambda cid: {"auto_load": auto_loads.get(cid, [])},
    )
    return auto_loads


def test_undocumented_platform_borrows_documented_auto_load(
    fake_platforms: dict[str, list[str]],
) -> None:
    """A target platform without its own docs page aliases to its documented dep."""
    fake_platforms["bk72xx"] = ["libretiny"]
    fake_platforms["rtl87xx"] = ["libretiny"]

    aliases = _shared_docs_page_aliases({"esp32", "libretiny"})

    assert aliases == {"bk72xx": "libretiny", "rtl87xx": "libretiny"}


def test_documented_platform_keeps_its_own_page(
    fake_platforms: dict[str, list[str]],
) -> None:
    """A platform with its own docs page is never aliased, whatever it auto-loads."""
    fake_platforms["esp32"] = ["libretiny"]

    assert "esp32" not in _shared_docs_page_aliases({"esp32", "libretiny"})


def test_platform_with_no_documented_dep_gets_no_alias(
    fake_platforms: dict[str, list[str]],
) -> None:
    """No documented ``auto_load`` dep means no alias rather than a guess."""
    fake_platforms["newchip"] = ["preferences"]

    assert "newchip" not in _shared_docs_page_aliases({"esp32", "libretiny"})


def test_multiple_documented_deps_warns_and_uses_first(
    fake_platforms: dict[str, list[str]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An ambiguous multi-dep platform takes the first dep and warns at sync time."""
    fake_platforms["newchip"] = ["libretiny", "esp32"]

    with caplog.at_level(logging.WARNING):
        aliases = _shared_docs_page_aliases({"esp32", "libretiny"})

    assert aliases["newchip"] == "libretiny"
    assert "auto-loads multiple documented components" in caplog.text
