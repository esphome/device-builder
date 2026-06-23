"""Sub-readings on multi-sensor platforms surface on the main form."""

from __future__ import annotations

import json
from pathlib import Path

from script.sync_components import (  # type: ignore[import-not-found]
    _PLATFORM_DOMAINS,
    _apply_auto_loaded_reference_advanced,
    _convert_field,
    _is_own_id_field,
    _mark_platform_domains_multi_conf,
    _multi_instance_targets,
    _resolve_auto_load,
)

_UNUSED_SCHEMA_DIR = Path("/unused")
_BODIES_DIR = (
    Path(__file__).resolve().parent.parent / "esphome_device_builder" / "definitions" / "components"
)


def _load_body(component_id: str) -> dict:
    """Read one component's split body file off disk."""
    return json.loads((_BODIES_DIR / f"{component_id}.json").read_text(encoding="utf-8"))


def _convert(raw: dict) -> dict:
    entry = _convert_field("temperature", raw, _UNUSED_SCHEMA_DIR)
    assert entry is not None
    return entry


def test_sub_reading_extends_overrides_advanced_to_false() -> None:
    """A field whose schema extends a sensor base lands not-advanced (#983)."""
    raw = {
        "key": "Optional",
        "type": "schema",
        "schema": {
            "config_vars": {"name": {"key": "Required"}},
            "extends": ["sensor._SENSOR_SCHEMA"],
        },
    }
    assert _convert(raw)["advanced"] is False


def test_binary_sensor_and_text_sensor_bases_also_override() -> None:
    """The override applies to all three multi-sensor base schemas."""
    for base in (
        "binary_sensor._BINARY_SENSOR_SCHEMA",
        "text_sensor._TEXT_SENSOR_SCHEMA",
    ):
        raw = {
            "key": "Optional",
            "type": "schema",
            "schema": {"config_vars": {}, "extends": [base]},
        }
        assert _convert(raw)["advanced"] is False, f"failed for {base}"


def test_non_sub_reading_nested_keeps_default_advanced() -> None:
    """A nested field that does NOT extend a sensor base stays as classified."""
    raw = {
        "key": "Optional",
        "type": "schema",
        "schema": {
            "config_vars": {"scan_window": {"key": "Optional"}},
            # Different base — e.g. a plain config block.
            "extends": ["esp32_ble_tracker._SCAN_PARAMETERS_SCHEMA"],
        },
    }
    # ``_classify_advanced`` defaults optional fields to advanced —
    # the override doesn't touch this case.
    assert _convert(raw)["advanced"] is True


def test_use_id_reference_with_generated_key_keeps_reference() -> None:
    """``cv.use_id`` cross-refs (``i2c_id``) keep ``references_component``.

    The schema wraps them in ``cv.GenerateID(...)`` so ``key`` is
    ``GeneratedID``, but ``use_id_type`` marks them as references, not
    the component's own id.
    """
    raw = {"key": "GeneratedID", "type": "use_id", "use_id_type": "i2c::I2CBus"}
    assert _is_own_id_field(raw) is False
    entry = _convert_field("i2c_id", raw, _UNUSED_SCHEMA_DIR)
    assert entry is not None
    assert entry["type"] == "id"
    assert entry["references_component"] == "i2c"


def test_generated_id_without_use_id_stays_own_id() -> None:
    """A bare ``GeneratedID`` (no ``use_id_type``) is still the own id."""
    assert _is_own_id_field({"key": "GeneratedID", "type": "use_id"}) is True


def test_no_extends_field_unaffected() -> None:
    """Fields with no ``extends`` reference are untouched by the override."""
    raw = {
        "key": "Optional",
        "type": "string",
    }
    # No extends → no override → falls back to ``_classify_advanced``.
    # ``temperature`` isn't in IMPORTANT_KEYS or ADVANCED_BASE_KEYS, so
    # ``_classify_advanced`` returns the default ``True`` for optionals.
    assert _convert(raw)["advanced"] is True


def test_catalog_dht_sub_readings_not_advanced() -> None:
    """Real catalog: DHT temperature + humidity surface on the main form."""
    dht = _load_body("sensor.dht")
    by_key = {e["key"]: e for e in dht["config_entries"]}
    # ``advanced: False`` is the default and gets stripped by
    # ``_strip_entry_defaults``; treat absent as False.
    assert by_key["temperature"].get("advanced", False) is False
    assert by_key["humidity"].get("advanced", False) is False


def test_catalog_debug_sub_readings_not_advanced_but_id_stays() -> None:
    """All 7 debug sub-readings surface; ``debug_id`` references the debug hub."""
    debug = _load_body("sensor.debug")
    by_key = {e["key"]: e for e in debug["config_entries"]}
    sub_readings = (
        "block",
        "cpu_frequency",
        "fragmentation",
        "free",
        "loop_time",
        "min_free",
        "psram",
    )
    for key in sub_readings:
        assert by_key[key].get("advanced", False) is False, f"{key} should not be advanced"
    # ``debug_id`` is a ``cv.use_id(DebugComponent)`` cross-reference
    # (``GenerateID`` key + ``use_id_type``), not the platform's own id;
    # it carries ``references_component`` and stays on the main form
    # (debug is a dependency the user adds, not auto-loaded by the sensor).
    assert by_key["debug_id"]["references_component"] == "debug"
    assert by_key["debug_id"].get("advanced", False) is False


def test_auto_loaded_reference_marked_advanced() -> None:
    """An auto-generated id reference to a self-AUTO_LOADed SINGLETON hides behind Advanced."""
    entries = [
        {"key": "web_server_base_id", "references_component": "web_server_base"},
        {"key": "modbus_id", "references_component": "modbus"},  # multi-instance target
        {"key": "i2c_id", "references_component": "i2c"},  # outside the closure
        {"key": "sensor", "references_component": "voltage_sampler", "required": True},
        {"key": "placeholder", "references_component": "image"},  # not an auto-id
        {"key": "name", "references_component": None},
    ]
    _apply_auto_loaded_reference_advanced(
        entries,
        {"web_server_base", "json", "modbus", "voltage_sampler", "image"},
        {"modbus"},
    )
    by_key = {e["key"]: e for e in entries}
    assert by_key["web_server_base_id"]["advanced"] is True
    assert "advanced" not in by_key["modbus_id"]
    assert "advanced" not in by_key["i2c_id"]
    assert "advanced" not in by_key["sensor"]
    assert "advanced" not in by_key["placeholder"]
    assert "advanced" not in by_key["name"]


def test_auto_loaded_reference_resorts_promoted_list() -> None:
    """Promoting a reference to advanced re-groups it after non-advanced siblings."""
    entries = [
        {"key": "web_server_base_id", "references_component": "web_server_base"},
        {"key": "port", "references_component": None},
    ]
    _apply_auto_loaded_reference_advanced(entries, {"web_server_base"}, set())
    assert [e["key"] for e in entries] == ["port", "web_server_base_id"]


def test_auto_loaded_reference_noop_on_empty_closure() -> None:
    """No AUTO_LOAD closure means nothing is forced advanced."""
    entries = [{"key": "web_server_base_id", "references_component": "web_server_base"}]
    _apply_auto_loaded_reference_advanced(entries, set(), set())
    assert "advanced" not in entries[0]


def test_multi_instance_targets_includes_provided_bases() -> None:
    """A multi-conf provider marks both its own name and the base it provides."""
    components = [
        {"id": "modbus", "multi_conf": True},
        {"id": "rc522_spi", "multi_conf": True, "provides": ["rc522"]},
        {"id": "web_server_base", "multi_conf": False},
        {"id": "sensor.dht", "multi_conf": False},
    ]
    multi = _multi_instance_targets(components)
    assert "modbus" in multi
    assert "rc522" in multi  # via its multi-conf provider
    assert "rc522_spi" in multi
    assert "switch" in multi  # platform domains are always multi-instance
    assert "web_server_base" not in multi


def test_mark_platform_domains_multi_conf() -> None:
    """Platform-domain entries flip to multi_conf; singletons stay untouched."""
    entries = [
        {"id": "output.libretiny_pwm"},
        {"id": "sensor.dht"},
        {"id": "wifi"},
        {"id": "bmi270.motion"},
        {"id": "i2c", "multi_conf": True},
    ]
    _mark_platform_domains_multi_conf(entries)
    by_id = {e["id"]: e for e in entries}
    assert by_id["output.libretiny_pwm"]["multi_conf"] is True
    assert by_id["sensor.dht"]["multi_conf"] is True
    assert "multi_conf" not in by_id["wifi"]  # bare singleton
    assert "multi_conf" not in by_id["bmi270.motion"]  # non-platform domain
    assert by_id["i2c"]["multi_conf"] is True


def test_shipped_platform_entries_are_multi_conf() -> None:
    """The shipped catalog marks platform variants repeatable (issue #1663)."""
    assert _load_body("output.libretiny_pwm")["multi_conf"] is True
    assert _load_body("sensor.dht")["multi_conf"] is True


def test_platform_domains_match_yaml_entity_categories() -> None:
    """``_PLATFORM_DOMAINS`` must equal the YAML serializer's ``_ENTITY_CATEGORIES``."""
    # If they drift, a platform domain stamped multi_conf=True here would fall into
    # the serializer's multi_conf branch and render invalid list-form ``<id>:`` YAML.
    from esphome_device_builder.helpers.yaml.component import (  # noqa: PLC0415
        _ENTITY_CATEGORIES,
    )

    assert set(_PLATFORM_DOMAINS) == set(_ENTITY_CATEGORIES)


def test_resolve_auto_load_handles_callable() -> None:
    """A callable AUTO_LOAD resolves to its list; one that can't run falls back to []."""
    assert _resolve_auto_load(["a", "b"]) == ["a", "b"]
    assert _resolve_auto_load(lambda: ["web_server_base"]) == ["web_server_base"]

    def needs_config() -> list[str]:
        raise RuntimeError("needs a real config")

    assert _resolve_auto_load(needs_config) == []
    assert _resolve_auto_load(None) == []


def test_resolve_auto_load_forces_and_restores_core_platform() -> None:
    """The callable runs platform-agnostic, and CORE's prior platform is restored."""
    from esphome.const import KEY_CORE, KEY_TARGET_PLATFORM  # noqa: PLC0415
    from esphome.core import CORE  # noqa: PLC0415

    core = CORE.data.setdefault(KEY_CORE, {})
    had, prev = KEY_TARGET_PLATFORM in core, core.get(KEY_TARGET_PLATFORM)
    core[KEY_TARGET_PLATFORM] = "esp32"
    try:
        seen: list[str | None] = []

        def auto_load() -> list[str]:
            seen.append(core[KEY_TARGET_PLATFORM])
            return ["web_server_base"]

        assert _resolve_auto_load(auto_load) == ["web_server_base"]
        assert seen == [None]  # forced agnostic during the call
        assert core[KEY_TARGET_PLATFORM] == "esp32"  # restored after
    finally:
        if had:
            core[KEY_TARGET_PLATFORM] = prev
        else:
            core.pop(KEY_TARGET_PLATFORM, None)


def test_catalog_singleton_base_ids_advanced_multi_stay_shown() -> None:
    """Singleton auto-loaded base ids hide; multi-instance pickers stay on the main form."""
    ws = {e["key"]: e for e in _load_body("web_server")["config_entries"]}
    assert ws["web_server_base_id"]["references_component"] == "web_server_base"
    assert ws["web_server_base_id"]["advanced"] is True
    # captive_portal AUTO_LOADs web_server_base via a callable; resolving it
    # lets the same rule hide its id picker too.
    cp = {e["key"]: e for e in _load_body("captive_portal")["config_entries"]}
    assert cp["web_server_base_id"]["references_component"] == "web_server_base"
    assert cp["web_server_base_id"]["advanced"] is True
    # modbus is MULTI_CONF, so modbus_controller's id picker stays visible.
    mc = {e["key"]: e for e in _load_body("modbus_controller")["config_entries"]}
    assert mc["modbus_id"]["references_component"] == "modbus"
    assert mc["modbus_id"].get("advanced", False) is False
    # spi allows multiple buses (cv.ensure_list) and esp32_ble_tracker is a
    # user-added dependency, not auto-created — both id pickers stay shown.
    spi = {e["key"]: e for e in _load_body("sensor.bmp280_spi")["config_entries"]}
    assert spi["spi_id"].get("advanced", False) is False
    ble = {e["key"]: e for e in _load_body("sensor.mopeka_pro_check")["config_entries"]}
    assert ble["esp32_ble_id"].get("advanced", False) is False
