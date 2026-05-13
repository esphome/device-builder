"""Tests for ``_resolve_download_component`` platform → module mapping.

The download endpoint asks ESPHome for the binary types available
for a given storage's ``target_platform`` by importing
``esphome.components.<X>`` and calling ``get_download_types``. The
mapping from ``target_platform`` to ``X`` isn't 1:1:

- ESP32 variants (``ESP32C3``, ``ESP32S3``, …) all live under the
  umbrella ``esp32`` component.
- LibreTiny chip families (such as ``rtl87xx``, ``bk72xx``,
  ``ln882x``) live under the umbrella ``libretiny`` component.
  The exhaustive set is sourced from upstream's
  ``FAMILY_COMPONENT.values()`` and grows automatically when a
  new chip family is added there.

Mirrors the inline mapping in
``esphome/dashboard/web_server.py``'s ``DownloadListRequestHandler``
— keep in sync. These tests pin the contract so a regression on
either side surfaces explicitly.
"""

from __future__ import annotations

import importlib

import pytest
from esphome.components.esp32 import VARIANTS as ESP32_VARIANTS

from esphome_device_builder.controllers.firmware.download import (
    _LIBRETINY_TARGET_PLATFORMS,
    _resolve_download_component,
)


@pytest.mark.parametrize("variant", sorted(ESP32_VARIANTS))
def test_esp32_variants_resolve_to_esp32(variant: str) -> None:
    """Every known ESP32 variant maps to the umbrella ``esp32`` component.

    Driven from ``ESP32_VARIANTS`` (imported from upstream) so the
    test breadth tracks upstream automatically — when ESPHome adds
    a new variant the parametrisation picks it up without an edit
    here.
    """
    assert _resolve_download_component(variant) == "esp32"
    # Lowercase form (which is how StorageJSON stores it after
    # ``.lower()``) also resolves correctly.
    assert _resolve_download_component(variant.lower()) == "esp32"


@pytest.mark.parametrize("family", sorted(_LIBRETINY_TARGET_PLATFORMS))
def test_libretiny_families_resolve_to_libretiny(family: str) -> None:
    """Every LibreTiny chip family maps to the umbrella ``libretiny`` component."""
    assert _resolve_download_component(family) == "libretiny"


def test_pass_through_for_first_class_platforms() -> None:
    """Platforms that are their own component name pass through unchanged.

    ``esp8266`` is a real ``esphome.components.esp8266`` package
    — no remapping needed; same for ``host`` and ``rp2040``.
    """
    assert _resolve_download_component("esp8266") == "esp8266"
    assert _resolve_download_component("rp2040") == "rp2040"
    assert _resolve_download_component("host") == "host"


def test_uppercase_first_class_platform_lowercased() -> None:
    """Mixed-case input is normalised to lowercase before lookup.

    ``StorageJSON.target_platform`` historically stored both forms
    (uppercase ``ESP8266`` from older sidecars, lowercase
    ``esp8266`` from newer writes). The resolver must produce the
    lowercase component name in either case.
    """
    assert _resolve_download_component("ESP8266") == "esp8266"
    assert _resolve_download_component("Rp2040") == "rp2040"


def test_unknown_platform_passes_through_lowercased() -> None:
    """Unknown platforms pass through lowercased.

    The resolver doesn't validate the component module exists —
    it just returns the lowercased input so the caller's
    ``importlib.import_module`` lookup fails in its own
    ``try/except`` and a warning is logged. Locks the "best
    effort" contract.
    """
    assert _resolve_download_component("unknownplat") == "unknownplat"
    assert _resolve_download_component("UnknownPlat") == "unknownplat"


def test_empty_platform_returns_empty_string() -> None:
    """Empty / ``None`` ``target_platform`` doesn't crash.

    ``StorageJSON.target_platform`` is itself nullable, so the
    resolver accepts ``str | None``. The caller's
    ``importlib.import_module`` then fails with ``ModuleNotFoundError``
    inside the controller's ``try/except`` and a warning is logged.
    """
    assert _resolve_download_component("") == ""
    assert _resolve_download_component(None) == ""


@pytest.mark.parametrize("family", sorted(_LIBRETINY_TARGET_PLATFORMS))
def test_libretiny_family_modules_actually_export_get_download_types(family: str) -> None:
    """The resolved ``libretiny`` module has the expected entry point.

    Sanity that the mapping doesn't just *look* right — the
    upstream ``libretiny`` component must still expose
    ``get_download_types`` (which is what the controller's
    runtime code calls). If upstream renames or drops it, this
    test catches the divergence at our level rather than letting
    the user hit "Could not determine download types" in the UI.
    """
    component = _resolve_download_component(family)
    module = importlib.import_module(f"esphome.components.{component}")
    assert callable(getattr(module, "get_download_types", None)), (
        f"esphome.components.{component} no longer exposes get_download_types — "
        f"upstream API changed; the dashboard's download endpoint will fail for "
        f"{family} configs"
    )


@pytest.mark.parametrize("component", ["esp32", "esp8266", "rp2040"])
def test_first_class_component_modules_export_get_download_types(component: str) -> None:
    """Sanity for the non-LibreTiny modules our resolver returns.

    Mirrors ``test_libretiny_family_modules_actually_export_get_download_types``
    for the components we route to directly (no umbrella). If
    upstream drops ``get_download_types`` from any of them, this
    fails immediately rather than waiting for a user to click
    Download in the dashboard and hit a runtime warning.
    """
    module = importlib.import_module(f"esphome.components.{component}")
    assert callable(getattr(module, "get_download_types", None)), (
        f"esphome.components.{component} no longer exposes get_download_types — "
        f"upstream API changed; the dashboard's download endpoint will fail for "
        f"{component} configs"
    )
