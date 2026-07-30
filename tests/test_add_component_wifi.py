"""Recovery defaults when ``devices/add_component`` adds the ``wifi`` component."""

from __future__ import annotations

from pathlib import Path

import pytest

from esphome_device_builder.controllers.components import ComponentCatalog
from tests.conftest import make_add_component_controller

pytestmark = pytest.mark.xdist_group("catalog")

_ESP32_YAML = """esphome:
  name: kitchen-lamp
  friendly_name: Kitchen Lamp

esp32:
  board: esp32dev
"""

_NRF52_YAML = """esphome:
  name: kitchen-lamp
  friendly_name: Kitchen Lamp

nrf52:
  board: adafruit_feather_nrf52840
"""


@pytest.fixture
def catalog(session_component_catalog: ComponentCatalog) -> ComponentCatalog:
    """Reuse the session-scoped catalog (board catalog already wired in)."""
    return session_component_catalog


def _write_secrets(config_dir: Path) -> None:
    (config_dir / "secrets.yaml").write_text(
        'wifi_ssid: "HomeNet"\nwifi_password: "hunter2"\n', "utf-8"
    )


async def test_empty_add_with_secrets_fills_recovery_block(
    catalog: ComponentCatalog, tmp_path: Path
) -> None:
    """An untouched wifi add lands the wizard-parity block, not a bare ``wifi:``."""
    (tmp_path / "lamp.yaml").write_text(_ESP32_YAML, "utf-8")
    _write_secrets(tmp_path)
    ctrl = make_add_component_controller(catalog, tmp_path)

    response = await ctrl.add_component(configuration="lamp.yaml", component_id="wifi", fields={})

    assert "  ssid: !secret wifi_ssid\n" in response.yaml
    assert "  password: !secret wifi_password\n" in response.yaml
    assert "  ap:\n" in response.yaml
    assert "    ssid: Kitchen Lamp Fallback Hotspot\n" in response.yaml
    ap_password = response.yaml.split("    password: ", 1)[1].split("\n", 1)[0]
    assert len(ap_password.strip('"')) == 12
    assert "\ncaptive_portal:" in response.yaml
    assert (tmp_path / "lamp.yaml").read_text("utf-8") == response.yaml


async def test_empty_add_without_secrets_stays_bare(
    catalog: ComponentCatalog, tmp_path: Path
) -> None:
    """Without shared Wi-Fi secrets the add keeps today's bare ``wifi:`` block."""
    (tmp_path / "lamp.yaml").write_text(_ESP32_YAML, "utf-8")
    ctrl = make_add_component_controller(catalog, tmp_path)

    response = await ctrl.add_component(configuration="lamp.yaml", component_id="wifi", fields={})

    assert response.yaml.rstrip().endswith("wifi:")
    assert "!secret" not in response.yaml
    assert "ap:" not in response.yaml
    assert "captive_portal:" not in response.yaml


async def test_typed_credentials_survive_and_gain_recovery(
    catalog: ComponentCatalog, tmp_path: Path
) -> None:
    """User-typed credentials stay inline; the fallback AP and portal still land."""
    (tmp_path / "lamp.yaml").write_text(_ESP32_YAML, "utf-8")
    _write_secrets(tmp_path)
    ctrl = make_add_component_controller(catalog, tmp_path)

    response = await ctrl.add_component(
        configuration="lamp.yaml",
        component_id="wifi",
        fields={"ssid": "MyNet", "password": "pw12345"},
    )

    assert "  ssid: MyNet\n" in response.yaml
    assert "  password: pw12345\n" in response.yaml
    assert "!secret wifi_ssid" not in response.yaml
    assert "  ap:\n" in response.yaml
    assert "\ncaptive_portal:" in response.yaml


async def test_secret_picker_values_emit_as_tags(catalog: ComponentCatalog, tmp_path: Path) -> None:
    """Picker-shaped ``!secret`` field values round-trip unquoted."""
    (tmp_path / "lamp.yaml").write_text(_ESP32_YAML, "utf-8")
    _write_secrets(tmp_path)
    ctrl = make_add_component_controller(catalog, tmp_path)

    response = await ctrl.add_component(
        configuration="lamp.yaml",
        component_id="wifi",
        fields={"ssid": "!secret other_ssid", "password": "!secret other_password"},
    )

    assert "  ssid: !secret other_ssid\n" in response.yaml
    assert "  password: !secret other_password\n" in response.yaml
    assert '"!secret' not in response.yaml


async def test_user_supplied_ap_is_preserved(catalog: ComponentCatalog, tmp_path: Path) -> None:
    """A caller-provided ``ap`` block is never overwritten by the fallback."""
    (tmp_path / "lamp.yaml").write_text(_ESP32_YAML, "utf-8")
    _write_secrets(tmp_path)
    ctrl = make_add_component_controller(catalog, tmp_path)

    response = await ctrl.add_component(
        configuration="lamp.yaml",
        component_id="wifi",
        fields={"ap": {"ssid": "My Recovery AP", "password": "custom123456"}},
    )

    assert "    ssid: My Recovery AP\n" in response.yaml
    assert "Fallback Hotspot" not in response.yaml
    assert "\ncaptive_portal:" in response.yaml


async def test_existing_wifi_block_is_untouched(catalog: ComponentCatalog, tmp_path: Path) -> None:
    """Re-adding wifi to a config that has it stays a no-op — no defaults, no portal."""
    existing = _ESP32_YAML + "\nwifi:\n  ssid: OldNet\n  password: oldpw\n"
    (tmp_path / "lamp.yaml").write_text(existing, "utf-8")
    _write_secrets(tmp_path)
    ctrl = make_add_component_controller(catalog, tmp_path)

    response = await ctrl.add_component(configuration="lamp.yaml", component_id="wifi", fields={})

    assert response.yaml == existing


async def test_non_captive_portal_platform_gets_credentials_only(
    catalog: ComponentCatalog, tmp_path: Path
) -> None:
    """A platform without captive-portal support fills credentials but no AP/portal."""
    (tmp_path / "lamp.yaml").write_text(_NRF52_YAML, "utf-8")
    _write_secrets(tmp_path)
    ctrl = make_add_component_controller(catalog, tmp_path)

    response = await ctrl.add_component(configuration="lamp.yaml", component_id="wifi", fields={})

    assert "  ssid: !secret wifi_ssid\n" in response.yaml
    assert "  password: !secret wifi_password\n" in response.yaml
    assert "ap:" not in response.yaml
    assert "captive_portal:" not in response.yaml


async def test_rp2040_alias_platform_counts_as_captive_capable(
    catalog: ComponentCatalog, tmp_path: Path
) -> None:
    """The ``rp2040:`` platform spelling normalises onto the captive-portal allowlist."""
    yaml = _ESP32_YAML.replace("esp32:\n  board: esp32dev", "rp2040:\n  board: rpipicow")
    (tmp_path / "lamp.yaml").write_text(yaml, "utf-8")
    _write_secrets(tmp_path)
    ctrl = make_add_component_controller(catalog, tmp_path)

    response = await ctrl.add_component(configuration="lamp.yaml", component_id="wifi", fields={})

    assert "  ap:\n" in response.yaml
    assert "\ncaptive_portal:" in response.yaml


async def test_captive_portal_not_duplicated(catalog: ComponentCatalog, tmp_path: Path) -> None:
    """A config that already has ``captive_portal:`` doesn't gain a second one."""
    existing = _ESP32_YAML + "\ncaptive_portal:\n"
    (tmp_path / "lamp.yaml").write_text(existing, "utf-8")
    _write_secrets(tmp_path)
    ctrl = make_add_component_controller(catalog, tmp_path)

    response = await ctrl.add_component(configuration="lamp.yaml", component_id="wifi", fields={})

    assert response.yaml.count("captive_portal:") == 1
    assert "  ap:\n" in response.yaml
