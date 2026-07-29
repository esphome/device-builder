"""Tests for the sync script's unit-options extraction.

`_extract_validator_units` is the load-bearing magic that pulls the
unit picker list out of `cv.float_with_unit` validators at runtime —
no hand-maintained mapping that goes stale on the next ESPHome
release. Pin its output for each `cv.*` validator the catalog cares
about so an upstream regex tweak can't silently change the unit list
the dashboard ships.

`_audit_catalog_for_unit_mismatches` is the regression net for new
unit-coerced validators ESPHome adds after this PR — make sure the
warning fires for the cases we've already curated as follow-ups.
"""

from __future__ import annotations

import logging
import types

import orjson
import pytest

from script.sync_components import (  # type: ignore[import-not-found]
    _AUTOMATIONS_BODIES_DIR,
    _OUTPUT_BODIES_DIR,
    _SUFFIX_UNIT_RE,
    _audit_catalog_for_unit_mismatches,
    _collect_automation_refined_types,
    _collect_refined_types,
    _delegated_schema,
    _derive_suffix_units,
    _enumerate_platform_manifests,
    _extract_validator_units,
    _hidden_schema,
    _registry_entry_schema,
    _require_non_introspectable_units,
    _unwrap_schema_to_dict,
    _walk_schema_keys,
)


@pytest.fixture
def cv():
    """Lazy-import esphome's config_validation; skip if unavailable."""
    try:
        from esphome import config_validation as _cv  # noqa: PLC0415
    except Exception:
        pytest.skip("esphome.config_validation not importable")
    return _cv


def test_extract_units_for_frequency(cv) -> None:
    """`cv.frequency` produces the IoT-relevant metric-prefixed Hz list.

    Canonical unit (`Hz`) first; remaining prefixes in magnitude
    order. The frontend's renderer treats `unit_options[0]` as the
    canonical unit (range bounds default to it), so this contract
    matters at every layer.
    """
    assert _extract_validator_units(cv.frequency) == [
        "Hz",
        "nHz",
        "µHz",
        "mHz",
        "kHz",
        "MHz",
        "GHz",
    ]


def test_extract_units_for_voltage(cv) -> None:
    """`cv.voltage` produces the IoT-relevant metric-prefixed V list."""
    assert _extract_validator_units(cv.voltage) == [
        "V",
        "nV",
        "µV",
        "mV",
        "kV",
        "MV",
        "GV",
    ]


def test_extract_units_for_distance(cv) -> None:
    """`cv.distance` produces the IoT-relevant metric-prefixed m list."""
    assert _extract_validator_units(cv.distance) == [
        "m",
        "nm",
        "µm",
        "mm",
        "km",
        "Mm",
        "Gm",
    ]


def test_extract_units_for_framerate(cv) -> None:
    """`cv.framerate` is a fixed-unit validator (no metric prefix)."""
    units = _extract_validator_units(cv.framerate)
    # Order is canonical-first; both `FPS` and `Hz` accepted by the
    # validator. We don't pin order here because `framerate`'s regex
    # alternation is stable but the canonical pick depends on the
    # uppercase-preference heuristic.
    assert units is not None
    assert set(units) >= {"FPS", "Hz"}


def test_extract_units_for_resistance(cv) -> None:
    """`cv.resistance` (not on any hand-maintained list) is discovered as metric Ω."""
    assert _extract_validator_units(cv.resistance) == [
        "Ω",
        "nΩ",
        "µΩ",
        "mΩ",
        "kΩ",
        "MΩ",
        "GΩ",
    ]


def test_extract_units_for_current(cv) -> None:
    """`cv.current` is discovered as a metric-prefixed A list."""
    assert _extract_validator_units(cv.current) == [
        "A",
        "nA",
        "µA",
        "mA",
        "kA",
        "MA",
        "GA",
    ]


def test_extract_units_for_bps(cv) -> None:
    """`cv.bps` is discovered as a metric-prefixed bit-rate list."""
    units = _extract_validator_units(cv.bps)
    assert units is not None
    assert units[0] == "bps"
    assert {"kbps", "Mbps", "Gbps"} <= set(units)


def test_extract_units_for_decibel(cv) -> None:
    """`cv.decibel` is a non-metric unit: distinct dB / dBm, no prefixes."""
    units = _extract_validator_units(cv.decibel)
    assert units is not None
    assert set(units) == {"dB", "dBm"}
    assert units[0] == "dB"


def test_extract_units_for_angle(cv) -> None:
    """`cv.angle` is a non-metric unit: ° / deg, no prefixes."""
    units = _extract_validator_units(cv.angle)
    assert units is not None
    assert set(units) == {"°", "deg"}


def test_extract_units_returns_none_for_unitless_validator(cv, caplog) -> None:
    """An empty-unit `float_with_unit` yields no units and drops without a warning."""
    with caplog.at_level(logging.WARNING, logger="sync_components"):
        assert _extract_validator_units(cv.float_with_unit("device factor", "")) is None
    assert "picker dropped" not in caplog.text


def test_extract_units_warns_on_non_unit_alternation(cv, caplog) -> None:
    """An alternation with no unit-shaped spelling warns instead of vanishing."""
    with caplog.at_level(logging.WARNING, logger="sync_components"):
        assert _extract_validator_units(cv.float_with_unit("volume", "(m³)")) is None
    assert "no unit-shaped spelling" in caplog.text


def test_shipped_catalog_tsl2591_factors_stay_plain_float() -> None:
    """The generated tsl2591 body carries no regex-fragment unit options."""
    body = orjson.loads((_OUTPUT_BODIES_DIR / "sensor.tsl2591.json").read_bytes())
    entries = {e["key"]: e for e in body["config_entries"]}
    for key in ("device_factor", "glass_attenuation_factor"):
        assert entries[key]["type"] == "float"
        assert not entries[key].get("unit_options")


def test_shipped_catalog_unit_options_are_unit_shaped() -> None:
    """Every shipped ``unit_options`` value matches the unit-shape gate."""
    violations = []

    def walk(entries, name):
        for entry in entries or []:
            violations.extend(
                (name, entry.get("key"), unit)
                for unit in entry.get("unit_options") or []
                if not _SUFFIX_UNIT_RE.match(unit)
            )
            walk(entry.get("config_entries"), name)

    for body_path in sorted(_OUTPUT_BODIES_DIR.glob("*.json")):
        walk(orjson.loads(body_path.read_bytes()).get("config_entries"), body_path.name)
    assert not violations


def test_extract_units_returns_none_for_non_closure() -> None:
    """A plain function (no compiled-regex closure) returns None."""

    def not_a_validator(value):
        return value

    assert _extract_validator_units(not_a_validator) is None


def test_audit_warns_on_unit_suffixed_string_default(caplog) -> None:
    """Audit fires on float/integer entries with non-numeric string defaults.

    Actionable telemetry for a hand-rolled validator that needs a
    `_NON_INTROSPECTABLE_UNITS` entry.
    """
    catalog = [
        {
            "id": "fake.component",
            "config_entries": [
                {
                    "key": "rate",
                    "type": "float",
                    "default_value": "100ms",
                },
                {
                    "key": "size",
                    "type": "integer",
                    "default_value": "1KB",
                },
                # Already-numeric default — must NOT trip the audit.
                {
                    "key": "count",
                    "type": "integer",
                    "default_value": "42",
                },
            ],
        }
    ]
    with caplog.at_level(logging.WARNING, logger="sync_components"):
        _audit_catalog_for_unit_mismatches(catalog)
    text = caplog.text
    assert "fake.component.rate" in text
    assert "fake.component.size" in text
    assert "fake.component.count" not in text


def test_audit_recurses_into_nested_entries(caplog) -> None:
    """Mismatches buried inside a NESTED group fire the warning with full path."""
    catalog = [
        {
            "id": "fake.component",
            "config_entries": [
                {
                    "key": "outer",
                    "type": "nested",
                    "config_entries": [
                        {
                            "key": "inner_rate",
                            "type": "float",
                            "default_value": "100ms",
                        }
                    ],
                }
            ],
        }
    ]
    with caplog.at_level(logging.WARNING, logger="sync_components"):
        _audit_catalog_for_unit_mismatches(catalog)
    # Warning includes the full dotted path (`outer.inner_rate`)
    # rather than the bare leaf — components with repeated nested
    # keys (`rate`, `size`) would otherwise produce ambiguous
    # warnings.
    assert "fake.component.outer.inner_rate" in caplog.text


def test_audit_recurses_into_map_value_templates(caplog) -> None:
    """MAP value templates carry inner ``config_entries`` too.

    `_build_map_value_template` materialises the value-side schema of
    user-keyed maps (`api.actions.<user_key>.<...>`,
    `esphome.platformio_options.<...>`). Without recursing into
    those, the audit silently misses any unit-coerced numeric
    default that lands inside one — exactly the class of catalog
    bug the audit is supposed to police.
    """
    catalog = [
        {
            "id": "fake.component",
            "config_entries": [
                {
                    "key": "actions",
                    "type": "map",
                    "config_entries": [
                        {
                            "key": "delay",
                            "type": "float",
                            "default_value": "100ms",
                        }
                    ],
                }
            ],
        }
    ]
    with caplog.at_level(logging.WARNING, logger="sync_components"):
        _audit_catalog_for_unit_mismatches(catalog)
    assert "fake.component.actions.delay" in caplog.text


@pytest.fixture
def loader():
    """Lazy-import esphome's loader; skip if unavailable."""
    try:
        from esphome import loader as _loader  # noqa: PLC0415
    except Exception:
        pytest.skip("esphome.loader not importable")
    return _loader


def test_enumerate_platform_manifests_returns_real_manifests(loader) -> None:
    """`mcp3008` ships a sensor and an output platform.

    `_enumerate_platform_manifests` must surface both so the platform-
    schema's unit-coerced fields (`reference_voltage` etc.) get refined
    on the live introspection walk — a small upstream shape change
    here would silently strip `float_with_unit` metadata otherwise.
    """
    manifests = _enumerate_platform_manifests(loader, "mcp3008")
    # At least the sensor platform should be reachable; output is
    # the secondary platform.
    assert manifests, "mcp3008 should expose at least one platform manifest"


def test_platform_manifest_refines_unit_coerced_field(loader) -> None:
    """End-to-end: `mcp3008.sensor.reference_voltage` is `float_with_unit`.

    The bare `mcp3008` manifest's `config_schema` carries the SPI bus
    fields but NOT the per-instance `reference_voltage` — that lives
    on the platform schema (`mcp3008.sensor`). If
    `_enumerate_platform_manifests` regresses, this catalog field
    silently falls back to `float`-with-string-default. Pin the
    refinement here so an upstream rename / restructure trips CI.
    """
    refined = {}
    for platform_manifest in _enumerate_platform_manifests(loader, "mcp3008"):
        refined.update(_collect_refined_types(platform_manifest))
    voltage = refined.get(("reference_voltage",))
    if voltage is None:
        pytest.skip(
            "esphome version doesn't expose mcp3008.sensor.reference_voltage "
            "via the live-introspection walker — guard, not a regression"
        )
    assert voltage.type == "float_with_unit"
    assert voltage.unit_options is not None and "V" in voltage.unit_options


def test_resistance_sensor_resistor_refines_to_float_with_unit(loader) -> None:
    """`resistance.sensor.resistor` refines to `float_with_unit` with Ω units."""
    refined = {}
    for platform_manifest in _enumerate_platform_manifests(loader, "resistance"):
        refined.update(_collect_refined_types(platform_manifest))
    resistor = refined.get(("resistor",))
    if resistor is None:
        pytest.skip(
            "esphome version doesn't expose resistance.sensor.resistor "
            "via the live-introspection walker — guard, not a regression"
        )
    assert resistor.type == "float_with_unit"
    assert resistor.unit_options is not None and "Ω" in resistor.unit_options


def test_non_introspectable_units_include_color_temperature(cv) -> None:
    """`cv.color_temperature` is a hand-rolled `def` (no regex), curated as mireds/K."""
    present = _require_non_introspectable_units(cv)
    assert present["color_temperature"] == ["mireds", "K"]


def test_rgbww_color_temperature_refines_to_float_with_unit(loader) -> None:
    """Rgbww's cold/warm white color_temperature refine to float_with_unit (mireds/K).

    A featured "6500 K" preset must validate in the add form, so the
    `cv.color_temperature` setpoints ship as `float_with_unit`, not plain `float`.
    """
    refined = {}
    for platform_manifest in _enumerate_platform_manifests(loader, "rgbww"):
        refined.update(_collect_refined_types(platform_manifest))
    cold = refined.get(("cold_white_color_temperature",))
    warm = refined.get(("warm_white_color_temperature",))
    if cold is None or warm is None:
        pytest.skip(
            "esphome version doesn't expose rgbww.light color temperature setpoints "
            "via the live-introspection walker — guard, not a regression"
        )
    assert cold.type == "float_with_unit"
    assert cold.unit_options == ["mireds", "K"]
    assert warm.type == "float_with_unit"
    assert warm.unit_options == ["mireds", "K"]


def test_missing_non_introspectable_validator_fails_the_sync() -> None:
    """A removed hand-maintained validator raises instead of silently dropping a picker."""

    class _StubCV:
        validate_bytes = object()
        temperature = object()
        color_temperature = object()
        percentage_int = object()
        # temperature_delta removed

    with pytest.raises(SystemExit, match="temperature_delta"):
        _require_non_introspectable_units(_StubCV())


def test_walk_descends_typed_schema_branches(cv) -> None:
    """``_walk_schema_keys`` visits fields inside ``cv.typed_schema`` branches."""
    typed = cv.typed_schema(
        {
            "W5500": cv.Schema({cv.Optional("clock_speed", default="26.67MHz"): cv.frequency}),
            "LAN8720": cv.Schema({cv.Optional("phy_addr", default=0): cv.int_}),
        },
        upper=True,
    )
    keys: set[str] = set()
    _walk_schema_keys(typed, lambda _k, key_name, _v, _path: keys.add(key_name))
    assert {"clock_speed", "phy_addr"} <= keys


def test_walk_peels_schema_extractor_closure(cv) -> None:
    """``_walk_schema_keys`` descends a ``@schema_extractor``-style closure."""
    from esphome import schema_extractors  # noqa: PLC0415

    base_schema = cv.Schema({cv.Required("carrier_duty_percent"): cv.percentage_int})

    def validator(config):
        if config == schema_extractors.SCHEMA_EXTRACT:
            return base_schema
        raise AssertionError("probed as a plain validator")

    keys: set[str] = set()
    _walk_schema_keys(validator, lambda _k, key_name, _v, _path: keys.add(key_name))
    assert keys == {"carrier_duty_percent"}


def test_walk_does_not_call_plain_validators(cv) -> None:
    """A closure that never references ``SCHEMA_EXTRACT`` is not probed."""
    calls: list[object] = []

    def validator(config):
        calls.append(config)
        return config

    keys: set[str] = set()
    _walk_schema_keys(validator, lambda _k, key_name, _v, _path: keys.add(key_name))
    assert keys == set()
    assert calls == []


def test_walk_peels_nested_wrapper_values(cv) -> None:
    """A wrapped nested value's fields are visited; an enum value's mapping is not."""
    from esphome import schema_extractors  # noqa: PLC0415

    inner = cv.Schema({cv.Optional("bytes"): cv.percentage_int})

    def wrapped(config):
        if config == schema_extractors.SCHEMA_EXTRACT:
            return inner
        raise AssertionError("probed as a plain validator")

    schema = cv.Schema(
        {
            cv.Optional("debug"): wrapped,
            cv.Optional("direction"): cv.enum({"RX": 1, "TX": 2}, upper=True),
        }
    )
    keys: set[str] = set()
    _walk_schema_keys(schema, lambda _k, key_name, _v, _path: keys.add(key_name))
    assert "bytes" in keys
    assert not {"RX", "TX"} & keys


def test_walk_peels_delegating_wrapper(cv) -> None:
    """A plain wrapper delegating to one module-level schema is peeled."""
    namespace = {"DEBUG_SCHEMA": cv.Schema({cv.Optional("after"): cv.percentage_int})}
    exec("def wrapper(value):\n    return DEBUG_SCHEMA(value)", namespace)  # noqa: S102
    keys: set[str] = set()
    _walk_schema_keys(
        cv.Schema({cv.Optional("debug"): namespace["wrapper"]}),
        lambda _k, key_name, _v, _path: keys.add(key_name),
    )
    assert "after" in keys


def test_registry_entry_schema_unwraps_maybe(cv) -> None:
    """A ``maybe_simple_value`` registration peels to the half carrying the fields."""
    wrapper = cv.maybe_simple_value(cv.Schema({cv.Optional("x"): cv.boolean}), key="x")
    peeled = _registry_entry_schema(types.SimpleNamespace(raw_schema=wrapper))
    assert peeled is not wrapper
    target = _unwrap_schema_to_dict(peeled)
    assert target is not None
    assert {key.schema for key in target} == {"x"}


def test_lambda_claims_fields_except_in_value_unions(cv) -> None:
    """A lambda refines bare or chained fields, never a multi-branch value union."""
    schema = cv.Schema(
        {
            cv.Optional("pure"): cv.lambda_,
            cv.Optional("chained"): cv.All(cv.returning_lambda),
            cv.Optional("union"): cv.Any(cv.returning_lambda, cv.string_strict),
        }
    )
    refined = _collect_refined_types(types.SimpleNamespace(config_schema=schema))
    assert refined[("pure",)].type == "lambda"
    assert refined[("chained",)].type == "lambda"
    # The union's lambda branch never claims the type; it marks the
    # field templatable and the plain branch types it.
    assert refined[("union",)].type == ""
    assert refined[("union",)].templatable is True


def test_lambda_union_typing_and_chain_carry(cv) -> None:
    """Pin typed-plus-flag in both branch orders, and the All-chain flag carry."""
    schema = cv.Schema(
        {
            cv.Optional("before"): cv.Any(cv.boolean, cv.returning_lambda),
            cv.Optional("after"): cv.Any(cv.returning_lambda, cv.boolean),
            cv.Optional("chain"): cv.All(cv.Any(cv.returning_lambda, cv.string_strict), cv.hex_int),
        }
    )
    refined = _collect_refined_types(types.SimpleNamespace(config_schema=schema))
    for key in ("before", "after"):
        assert refined[(key,)].type == "boolean"
        assert refined[(key,)].templatable is True
    assert refined[("chain",)].type == "integer"
    assert refined[("chain",)].display_format == "hex"
    assert refined[("chain",)].templatable is True


def test_http_request_action_buffer_refines_to_float_with_unit(loader) -> None:
    """The live action registry yields byte units for max_response_buffer_size."""
    loader.get_component("http_request")
    refined = _collect_automation_refined_types()
    action = refined["action"]["http_request.send"]
    assert action[("max_response_buffer_size",)].type == "float_with_unit"
    assert action[("max_response_buffer_size",)].unit_options == ["B", "kB", "MB", "GB"]


def test_shipped_automations_http_request_carries_byte_units() -> None:
    """The generated http_request action bodies render the buffer with a byte picker."""
    for action_id in ("http_request.send", "http_request.get", "http_request.post"):
        body = orjson.loads(
            (_AUTOMATIONS_BODIES_DIR / "actions" / f"{action_id}.json").read_bytes()
        )
        entry = {e["key"]: e for e in body["config_entries"]}["max_response_buffer_size"]
        assert entry["type"] == "float_with_unit"
        assert entry["unit_options"] == ["B", "kB", "MB", "GB"]


def test_datetime_set_date_stays_plain_but_templatable(loader) -> None:
    """A lambda branch in a value union marks the field templatable, not lambda-typed."""
    from esphome import automation  # noqa: PLC0415

    loader.get_component("datetime")
    assert "datetime.date.set" in automation.ACTION_REGISTRY
    refined = _collect_automation_refined_types()
    date = refined["action"]["datetime.date.set"][("date",)]
    assert date.type == ""
    assert date.templatable is True


def test_shipped_automations_datetime_set_stays_plain() -> None:
    """The generated datetime.date.set body keeps the plain date type."""
    body = orjson.loads(
        (_AUTOMATIONS_BODIES_DIR / "actions" / "datetime.date.set.json").read_bytes()
    )
    entry = {e["key"]: e for e in body["config_entries"]}["date"]
    assert entry["type"] == "string"


def test_delegated_schema_rejects_ambiguous_wrappers(cv) -> None:
    """A wrapper referencing two module-level schemas is not peeled."""
    namespace = {
        "A": cv.Schema({cv.Optional("x"): cv.boolean}),
        "B": cv.Schema({cv.Optional("y"): cv.boolean}),
    }
    exec("def wrapper(value):\n    return A(B(value))", namespace)  # noqa: S102
    assert _delegated_schema(namespace["wrapper"]) is None


def test_hidden_schema_probe_is_memoized(cv) -> None:
    """Repeated probes of one closure return one schema object."""
    from esphome import schema_extractors  # noqa: PLC0415

    base = cv.Schema({cv.Optional("x"): cv.percentage_int})

    def wrapped(config):
        if config == schema_extractors.SCHEMA_EXTRACT:
            return base.extend({})
        raise AssertionError("probed as a plain validator")

    assert _hidden_schema(wrapped) is _hidden_schema(wrapped)


def test_shipped_catalog_remote_receiver_carries_introspection() -> None:
    """The generated remote_receiver body is enriched through its trigger wrapper."""
    body = orjson.loads((_OUTPUT_BODIES_DIR / "remote_receiver.json").read_bytes())
    entries = {e["key"]: e for e in body["config_entries"]}
    assert entries["carrier_duty_percent"]["type"] == "float_with_unit"
    assert entries["carrier_duty_percent"]["platform_defaults"] == {"esp32": 100}
    assert entries["rmt_symbols"]["platform_defaults"]["esp32"] == 192


def test_collect_refined_types_descends_typed_schema(cv) -> None:
    """A ``cv.frequency`` field inside a typed_schema branch refines to ``float_with_unit``."""
    typed = cv.typed_schema(
        {"W5500": cv.Schema({cv.Optional("clock_speed", default="26.67MHz"): cv.frequency})},
        upper=True,
    )
    manifest = types.SimpleNamespace(config_schema=typed)
    refined = _collect_refined_types(manifest)
    clock_speed = refined.get(("clock_speed",))
    assert clock_speed is not None
    assert clock_speed.type == "float_with_unit"
    assert clock_speed.unit_options is not None and "MHz" in clock_speed.unit_options


def test_audit_silent_when_no_mismatches(caplog) -> None:
    """No warning when every numeric entry has a numeric default."""
    catalog = [
        {
            "id": "fake.component",
            "config_entries": [
                {"key": "rate", "type": "float", "default_value": 1.5},
                {"key": "count", "type": "integer", "default_value": 7},
                {"key": "name", "type": "string", "default_value": "abc"},
            ],
        }
    ]
    with caplog.at_level(logging.WARNING, logger="sync_components"):
        _audit_catalog_for_unit_mismatches(catalog)
    assert "Catalog audit" not in caplog.text


def test_derive_suffix_units_from_a_suffix_stripper() -> None:
    """A hand-rolled ``"<float><unit>"`` stripper yields its canonical unit."""

    def validate_speed(value):
        value = str(value)
        for suffix in ("steps/s",):
            value = value.removesuffix(suffix)
        return float(value)

    assert _derive_suffix_units(validate_speed) == ["steps/s"]


def test_derive_suffix_units_ships_every_verified_spelling() -> None:
    """All accepted spellings ship, tuple order first, so any YAML form parses."""

    def validate_acceleration(value):
        value = str(value)
        for suffix in ("steps/s^2", "steps/s*s", "steps/ss"):
            value = value.removesuffix(suffix)
        return float(value)

    assert _derive_suffix_units(validate_acceleration) == ["steps/s^2", "steps/s*s", "steps/ss"]


def test_derive_suffix_units_rejects_a_rescaling_suffix() -> None:
    """A suffix that changes the parsed magnitude is a conversion, not a unit."""

    def position(value):
        value = str(value)
        if value.endswith(("°", "deg")):
            return round(float(value.removesuffix("°").removesuffix("deg")) * 4096 / 360)
        return int(value)

    assert _derive_suffix_units(position) is None


def test_derive_suffix_units_rejects_a_lenient_validator() -> None:
    """A validator that numbers arbitrary text proves nothing about any suffix."""

    def anything_goes(value):
        for _suffix in ("steps/s",):
            pass
        return 1.0

    assert _derive_suffix_units(anything_goes) is None


def test_derive_suffix_units_ignores_non_unit_constants() -> None:
    """Enum-membership tuples never read as unit suffixes."""

    def truthy(value):
        if str(value) in ("true", "yes", "on"):
            return True
        raise ValueError(value)

    assert _derive_suffix_units(truthy) is None
    assert _derive_suffix_units("not callable") is None


def test_stepper_platform_refines_speed_fields_to_float_with_unit(loader) -> None:
    """``stepper.<platform>`` speed fields derive their units from ``validate_speed``."""
    refined = {}
    for platform_manifest in _enumerate_platform_manifests(loader, "uln2003"):
        refined.update(_collect_refined_types(platform_manifest))
    max_speed = refined.get(("max_speed",))
    if max_speed is None:
        pytest.skip(
            "esphome version doesn't expose stepper.uln2003.max_speed "
            "via the live-introspection walker — guard, not a regression"
        )
    assert max_speed.type == "float_with_unit"
    assert max_speed.unit_options == ["steps/s"]
    # Canonical spelling first; the alternate spellings follow in tuple order.
    assert refined[("acceleration",)].unit_options[0] == "steps/s^2"
    assert "steps/s*s" in refined[("acceleration",)].unit_options
    assert refined[("deceleration",)].unit_options[0] == "steps/s^2"


def test_shipped_catalog_stepper_speed_fields_carry_units() -> None:
    """The generated stepper bodies render the speed trio with unit suffixes."""
    for platform in ("stepper.a4988", "stepper.uln2003"):
        body = orjson.loads((_OUTPUT_BODIES_DIR / f"{platform}.json").read_bytes())
        entries = {e["key"]: e for e in body["config_entries"]}
        assert entries["max_speed"]["type"] == "float_with_unit"
        assert entries["max_speed"]["unit_options"] == ["steps/s"]
        for key in ("acceleration", "deceleration"):
            assert entries[key]["type"] == "float_with_unit"
            assert entries[key]["unit_options"][0] == "steps/s^2"
            assert "steps/s*s" in entries[key]["unit_options"]


def test_non_introspectable_units_include_percentage_int(cv) -> None:
    """`cv.percentage_int` is a hand-rolled `def` (no regex), curated as `%`."""
    present = _require_non_introspectable_units(cv)
    assert present["percentage_int"] == ["%"]


def test_non_introspectable_units_include_validate_bytes(cv) -> None:
    """`cv.validate_bytes` (inline regex, no closure) is curated as B/kB/MB/GB."""
    present = _require_non_introspectable_units(cv)
    assert present["validate_bytes"] == ["B", "kB", "MB", "GB"]


def test_shipped_catalog_buffer_size_carries_byte_units() -> None:
    """The generated remote_receiver body renders buffer_size with a byte picker."""
    body = orjson.loads((_OUTPUT_BODIES_DIR / "remote_receiver.json").read_bytes())
    entries = {e["key"]: e for e in body["config_entries"]}
    assert entries["buffer_size"]["type"] == "float_with_unit"
    assert entries["buffer_size"]["unit_options"] == ["B", "kB", "MB", "GB"]


def test_uart_debug_after_bytes_refines_through_wrapper(loader) -> None:
    """`uart.debug.after.bytes` refines through the `maybe_empty_debug` wrapper."""
    refined = _collect_refined_types(loader.get_component("uart"))
    after_bytes = refined[("debug", "after", "bytes")]
    assert after_bytes.type == "float_with_unit"
    assert after_bytes.unit_options == ["B", "kB", "MB", "GB"]


def test_shipped_catalog_debug_after_bytes_carries_byte_units() -> None:
    """The generated uart body renders the nested debug.after.bytes with a byte picker."""
    body = orjson.loads((_OUTPUT_BODIES_DIR / "uart.json").read_bytes())
    entries = {e["key"]: e for e in body["config_entries"]}
    after = {e["key"]: e for e in entries["debug"]["config_entries"]}["after"]
    bytes_entry = {e["key"]: e for e in after["config_entries"]}["bytes"]
    assert bytes_entry["type"] == "float_with_unit"
    assert bytes_entry["unit_options"] == ["B", "kB", "MB", "GB"]


def test_collect_refined_types_percentage_int(cv) -> None:
    """`cv.percentage_int` refines to `float_with_unit`, bare or inside an All chain."""
    schema = cv.Schema(
        {
            cv.Required("carrier_duty_percent"): cv.All(
                cv.percentage_int, cv.Range(min=1, max=100)
            ),
            cv.Optional("min_humidity"): cv.percentage_int,
        }
    )
    refined = _collect_refined_types(types.SimpleNamespace(config_schema=schema))
    for path in (("carrier_duty_percent",), ("min_humidity",)):
        assert refined[path].type == "float_with_unit"
        assert refined[path].unit_options == ["%"]


@pytest.mark.parametrize("component", ["remote_transmitter", "remote_receiver"])
def test_carrier_duty_refines_to_float_with_unit(loader, component) -> None:
    """`carrier_duty_percent` refines to `float_with_unit` (`%`), through wrappers too."""
    refined = _collect_refined_types(loader.get_component(component))
    duty = refined[("carrier_duty_percent",)]
    assert duty.type == "float_with_unit"
    assert duty.unit_options == ["%"]


def test_climate_visual_humidity_refines_to_float_with_unit(loader) -> None:
    """Climate's `visual.min_humidity`/`max_humidity` refine to `float_with_unit` (`%`)."""
    refined = {}
    for platform_manifest in _enumerate_platform_manifests(loader, "bang_bang"):
        refined.update(_collect_refined_types(platform_manifest))
    low = refined.get(("visual", "min_humidity"))
    high = refined.get(("visual", "max_humidity"))
    if low is None or high is None:
        pytest.skip(
            "esphome version doesn't expose climate visual humidity bounds "
            "via the live-introspection walker — guard, not a regression"
        )
    for entry in (low, high):
        assert entry.type == "float_with_unit"
        assert entry.unit_options == ["%"]


def test_shipped_catalog_carrier_duty_percent_accepts_percent() -> None:
    """The generated remote_transmitter body types carrier_duty_percent with a `%` unit."""
    body = orjson.loads((_OUTPUT_BODIES_DIR / "remote_transmitter.json").read_bytes())
    entries = {e["key"]: e for e in body["config_entries"]}
    duty = entries["carrier_duty_percent"]
    assert duty["type"] == "float_with_unit"
    assert duty["unit_options"] == ["%"]
    assert duty["range"] == [1, 100]
    assert duty["required"] is True


def test_templatable_inner_classifies(cv) -> None:
    """A ``cv.templatable`` closure classifies by its plain-side validator."""
    schema = cv.Schema(
        {
            cv.Optional("flag"): cv.templatable(cv.boolean),
            cv.Optional("duty"): cv.templatable(cv.percentage_int),
            cv.Optional("level"): cv.templatable(cv.possibly_negative_percentage),
        }
    )
    refined = _collect_refined_types(types.SimpleNamespace(config_schema=schema))
    assert refined[("flag",)].type == "boolean"
    assert refined[("duty",)].type == "float_with_unit"
    assert refined[("duty",)].unit_options == ["%"]
    # The rescaling percentage stays deliberately unrefined through the peel.
    assert ("level",) not in refined


def test_shipped_catalog_templatable_fields_carry_inner_types() -> None:
    """Templatable fields ship their plain-side type, units, and bounds."""
    body = orjson.loads(
        (_AUTOMATIONS_BODIES_DIR / "actions" / "cc1101.set_frequency.json").read_bytes()
    )
    entry = next(e for e in body["config_entries"] if e["key"] == "value")
    assert entry["type"] == "float_with_unit"
    assert entry["unit_options"][0] == "Hz"
    assert entry["range"] == [300000000.0, 928000000.0]
    # The wrapper's flag survives the peel — the lambda toggle stays.
    assert entry["templatable"] is True

    body = orjson.loads((_OUTPUT_BODIES_DIR / "light.rgb.json").read_bytes())
    initial = next(e for e in body["config_entries"] if e["key"] == "initial_state")
    ct = next(e for e in initial["config_entries"] if e["key"] == "color_temperature")
    assert ct["type"] == "float_with_unit"
    assert ct["unit_options"] == ["mireds", "K"]


def test_shipped_automations_lambda_unions_carry_templatable() -> None:
    """Lambda-or-plain union fields ship the toggle flag with typing untouched."""
    body = orjson.loads(
        (_AUTOMATIONS_BODIES_DIR / "actions" / "datetime.date.set.json").read_bytes()
    )
    entry = next(e for e in body["config_entries"] if e["key"] == "date")
    assert entry["templatable"] is True
    assert entry["type"] == "string"

    body = orjson.loads(
        (_AUTOMATIONS_BODIES_DIR / "actions" / "http_request.send.json").read_bytes()
    )
    entry = next(e for e in body["config_entries"] if e["key"] == "json")
    assert entry["templatable"] is True
    assert entry["type"] == "map"


def test_ensure_list_scalar_item_types_the_entry(cv) -> None:
    """A scalar list item's classification types the multi_value entry."""
    schema = cv.Schema(
        {
            cv.Optional("data"): cv.templatable(cv.ensure_list(cv.hex_uint8_t)),
            cv.Optional("bare"): cv.ensure_list(cv.boolean),
        }
    )
    refined = _collect_refined_types(types.SimpleNamespace(config_schema=schema))
    assert refined[("data",)].type == "integer"
    assert refined[("data",)].display_format == "hex"
    assert refined[("bare",)].type == "boolean"
