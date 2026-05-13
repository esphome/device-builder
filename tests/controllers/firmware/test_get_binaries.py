"""End-to-end coverage for ``FirmwareController.get_binaries``.

The handler reads ``StorageJSON`` for *configuration*, picks the
right ``esphome.components.<platform>`` module, and returns
whatever its ``get_download_types(storage)`` produces. The
configuration-traversal branch is already covered in
``test_traversal_validation.py``; this file pins:

- The five behavioural branches the handler implements
  (no sidecar / unknown platform / esp32 variants /
  libretiny families / general failure).
- The platform-resolution table inside
  ``_resolve_download_component``. The parametrisation pulls
  directly from ``esphome.components.esp32.VARIANTS`` and
  ``_LIBRETINY_TARGET_PLATFORMS`` (which is itself sourced from
  upstream's ``FAMILY_COMPONENT.values()``), so a new ESP32
  variant or LibreTiny family in upstream auto-shows up as a
  parametrised case here without an inline list edit.
- The result-list pass-through is honest — whatever the upstream
  module returns is what the WS client sees.
"""

from __future__ import annotations

import logging
import sys
import types
from pathlib import Path
from typing import Any

import esphome.components as parent
import pytest
from esphome.components.esp32 import VARIANTS as _ESP32_VARIANTS

from esphome_device_builder.controllers.firmware.download import (
    _LIBRETINY_TARGET_PLATFORMS,
    _resolve_download_component,
)
from tests._storage_fixtures import write_storage_json
from tests.controllers.firmware.conftest import FirmwareControllerFactory


@pytest.fixture(autouse=True)
def _redirect_ext_storage_path(monkeypatch: Any, tmp_path: Path) -> None:
    """Pin ``ext_storage_path`` at ``<tmp>/.esphome/storage/<config>.json``.

    Same redirect ``test_download.py`` uses — ``CORE.config_path``
    isn't initialised in the test process, so the controller-side
    binding gets the tmpfs layout instead.
    """
    monkeypatch.setattr(
        "esphome_device_builder.controllers.firmware.download.resolve_storage_path",
        lambda configuration: tmp_path / ".esphome" / "storage" / f"{configuration}.json",
    )


def _install_fake_component(
    monkeypatch: Any, module_name: str, types_returned: list[dict]
) -> list[Any]:
    """Register a fake ``esphome.components.<module_name>`` for the duration of a test.

    Returns the captured-call list so the test can assert
    ``get_download_types`` was actually invoked with the loaded
    storage.

    Patches *both* the ``sys.modules`` entry and the parent-package
    attribute on ``esphome.components``. The import system caches
    submodules on the parent package alongside the ``sys.modules``
    map; teardown only restoring ``sys.modules`` would leave the
    fake module visible as ``esphome.components.<module_name>``
    attribute access in later tests, which can break a downstream
    ``from esphome.components import esp32`` lookup.
    """
    captured: list[Any] = []

    def _get_download_types(storage: Any) -> list[dict]:
        captured.append(storage)
        return list(types_returned)

    fake = types.ModuleType(f"esphome.components.{module_name}")
    fake.get_download_types = _get_download_types  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, f"esphome.components.{module_name}", fake)
    # The parent package may or may not have the submodule loaded
    # already; ``raising=False`` makes setattr work in either case
    # and ``monkeypatch`` will undo the assignment (or delete the
    # attribute if it didn't exist before) on teardown.
    monkeypatch.setattr(parent, module_name, fake, raising=False)
    return captured


# ---------------------------------------------------------------------------
# _resolve_download_component
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("variant", sorted(_ESP32_VARIANTS))
def test_resolve_download_component_routes_every_esp32_variant_to_umbrella(
    variant: str,
) -> None:
    """Every ESP32 variant in upstream's ``VARIANTS`` collapses to ``"esp32"``.

    Drives the parametrization off ``esphome.components.esp32.VARIANTS``
    directly so an upstream variant addition (next ESP32 chip ESPHome
    supports) is automatically covered — no manual list update here.
    Both the canonical upper-case form and a lower-case round-trip
    are checked since ``StorageJSON`` sometimes stores the
    lower-cased value.
    """
    assert _resolve_download_component(variant) == "esp32"
    assert _resolve_download_component(variant.lower()) == "esp32"


@pytest.mark.parametrize("family", sorted(_LIBRETINY_TARGET_PLATFORMS))
def test_resolve_download_component_routes_every_libretiny_family_to_umbrella(
    family: str,
) -> None:
    """Every LibreTiny family in ``_LIBRETINY_TARGET_PLATFORMS`` routes to ``"libretiny"``.

    The set is built from upstream's ``FAMILY_COMPONENT.values()``
    plus the umbrella ``"libretiny"`` name — driving the test off
    that same set means a new LibreTiny chip family appearing in
    upstream's auto-generated mapping is automatically covered.
    """
    assert _resolve_download_component(family) == "libretiny"


@pytest.mark.parametrize("platform", ["rp2040", "host", "rtl8710b-unknown-vendor"])
def test_resolve_download_component_passes_unmapped_platforms_through(
    platform: str,
) -> None:
    """Non-mapped platforms pass through verbatim.

    The caller's ``importlib.import_module`` then resolves
    ``esphome.components.<platform>`` directly — covers the long
    tail of single-component platforms (``rp2040``, ``host``,
    future additions) that don't share a module with siblings.
    """
    assert _resolve_download_component(platform) == platform


def test_resolve_download_component_handles_none() -> None:
    """Nullable ``StorageJSON.target_platform`` flows through without an explicit coerce.

    The caller passes ``storage.target_platform`` directly; without
    the inline ``(target_platform or "")``, a sidecar where the
    field was never set would raise ``AttributeError`` on
    ``.lower()``. Pinning the empty-string fallthrough here so a
    refactor that drops the coalescing surfaces immediately.
    """
    assert _resolve_download_component(None) == ""
    assert _resolve_download_component("") == ""


# ---------------------------------------------------------------------------
# get_binaries — failure / fallback branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_binaries_returns_empty_when_storage_missing(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """No StorageJSON sidecar → empty list, NOT a raise.

    Distinct contract from ``download``: the dashboard's "Web
    Serial install" picker calls ``get_binaries`` for every device
    in the listing on render to decide which devices show a flash
    button. Raising for never-compiled devices would torpedo the
    whole listing; returning ``[]`` lets the picker show "compile
    first" inline.
    """
    controller = firmware_controller_factory()

    result = await controller.get_binaries(configuration="kitchen.yaml")

    assert result == []


@pytest.mark.asyncio
async def test_get_binaries_returns_empty_when_target_platform_missing(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """Sidecar exists but ``target_platform`` is empty → empty list.

    A truncated sidecar (write-then-crash mid-compile) can land
    with ``esp_platform`` unset. ``importlib.import_module`` of
    ``esphome.components.`` (empty path component) raises
    ``ImportError``; the handler swallows that and returns ``[]``
    so the listing keeps rendering.
    """
    write_storage_json(tmp_path, "kitchen.yaml", overrides={"esp_platform": ""})
    controller = firmware_controller_factory()

    result = await controller.get_binaries(configuration="kitchen.yaml")

    assert result == []


@pytest.mark.asyncio
async def test_get_binaries_logs_and_returns_empty_on_module_failure(
    tmp_path: Path,
    caplog: Any,
    monkeypatch: Any,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A module that raises from ``get_download_types`` → empty list + warning.

    Defense-in-depth: an ESPHome upstream change that breaks
    ``get_download_types`` for a given platform (raises an
    exception for some unhandled storage shape) shouldn't take
    down the listing for unrelated devices. Pin the warning log
    so an operator notices the regression in the dashboard log
    rather than seeing silent empty rows everywhere.
    """

    def _boom(_storage: Any) -> list[dict]:
        raise RuntimeError("upstream regression")

    fake = types.ModuleType("esphome.components.esp32")
    fake.get_download_types = _boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "esphome.components.esp32", fake)

    write_storage_json(tmp_path, "kitchen.yaml", overrides={"esp_platform": "esp32c3"})
    controller = firmware_controller_factory()

    with caplog.at_level(logging.WARNING):
        result = await controller.get_binaries(configuration="kitchen.yaml")

    assert result == []
    assert any(
        "Could not determine download types for kitchen.yaml" in rec.message
        for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# get_binaries — happy paths through each platform branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_binaries_routes_esp32_variants_through_umbrella_module(
    tmp_path: Path, monkeypatch: Any, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """ESP32 variant in storage → loads the umbrella ``esp32`` component.

    The flat ``esphome.components.esp32`` exposes
    ``get_download_types`` that knows about every variant. Without
    the variant→umbrella mapping the handler would try to import
    ``esphome.components.esp32c3`` (the variant's own component
    module, which exists but doesn't expose
    ``get_download_types``) and silently fall back to ``[]``.
    """
    captured = _install_fake_component(
        monkeypatch,
        "esp32",
        [{"title": "Modern (Web Serial)", "file": "firmware-factory.bin"}],
    )

    write_storage_json(
        tmp_path,
        "kitchen.yaml",
        overrides={"esp_platform": "esp32c3"},
    )
    controller = firmware_controller_factory()

    result = await controller.get_binaries(configuration="kitchen.yaml")

    assert result == [{"title": "Modern (Web Serial)", "file": "firmware-factory.bin"}]
    # ``get_download_types`` was actually called with the loaded storage,
    # not a stale or duplicated reference.
    assert len(captured) == 1
    assert captured[0].name == "kitchen"


@pytest.mark.asyncio
async def test_get_binaries_routes_libretiny_families_through_umbrella_module(
    tmp_path: Path, monkeypatch: Any, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """A LibreTiny family target loads the ``libretiny`` component, not the chip module.

    ``bk72xx`` is the broadest family — pinning it covers the
    common case. The keep-in-sync mechanism for the family list
    is exercised by ``test_resolve_download_component_table``
    above; this is the integration check that the routing
    actually reaches the right module.
    """
    captured = _install_fake_component(
        monkeypatch,
        "libretiny",
        [{"title": "LibreTiny RBL", "file": "firmware.rbl"}],
    )

    write_storage_json(
        tmp_path,
        "kitchen.yaml",
        overrides={"esp_platform": "bk72xx"},
    )
    controller = firmware_controller_factory()

    result = await controller.get_binaries(configuration="kitchen.yaml")

    assert result == [{"title": "LibreTiny RBL", "file": "firmware.rbl"}]
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_get_binaries_returns_module_list_verbatim(
    tmp_path: Path, monkeypatch: Any, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """The upstream module's list is returned verbatim — no filtering, no re-shaping.

    Pin the pass-through so a refactor that adds a "drop entries
    without an OTA bin" filter (or any other transform) shows up
    as a contract change in the test diff. The frontend's flash
    picker maps over each entry's ``title``/``file`` directly;
    silently dropping entries would hide install options from the
    user.
    """
    expected = [
        {"title": "Modern (Web Serial)", "file": "firmware-factory.bin"},
        {"title": "OTA Update", "file": "firmware.ota.bin"},
        {"title": "Boot App 0", "file": "boot_app0.bin"},
    ]
    _install_fake_component(monkeypatch, "esp32", expected)

    write_storage_json(
        tmp_path,
        "kitchen.yaml",
        overrides={"esp_platform": "esp32"},
    )
    controller = firmware_controller_factory()

    result = await controller.get_binaries(configuration="kitchen.yaml")

    assert result == expected
