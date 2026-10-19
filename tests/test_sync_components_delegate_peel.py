"""Sweep pinning every live delegate-wrapper peel to its intended schema."""

from __future__ import annotations

import importlib
import json

import esphome.config_validation as cv
import pytest

from script.sync_components import (  # type: ignore[import-not-found]
    _OUTPUT_BODIES_DIR,
    _delegated_schema,
    _get_esphome_loader,
    _hidden_schema,
    _unwrap_schema_to_dict,
)

# Every wrapper the delegate probe fires on across the installed tree
# (components, platforms, and automation registries), mapped to the
# module-level schema it must peel to.
_DELEGATE_WRAPPERS = [
    ("esphome.components.api", "_encryption_schema", "ENCRYPTION_SCHEMA"),
    ("esphome.components.climate", "visual_temperature_step", "VISUAL_TEMPERATURE_STEP_SCHEMA"),
    ("esphome.components.deep_sleep", "validate_wakeup_pin", "WAKEUP_PIN_SCHEMA"),
    ("esphome.components.micro_wake_word", "_maybe_empty_vad_schema", "VAD_MODEL_SCHEMA"),
    ("esphome.components.nrf52", "_dfu_schema", "_DFU_SCHEMA"),
    ("esphome.components.uart", "maybe_empty_debug", "DEBUG_SCHEMA"),
    ("esphome.components.wifi", "_fast_connect_schema", "FAST_CONNECT_SCHEMA"),
    ("esphome.components.wifi", "wifi_network_ap", "WIFI_NETWORK_AP"),
    ("esphome.core.config", "validate_area_config", "AREA_SCHEMA"),
]

# Wrappers that answer the ``SCHEMA_EXTRACT`` probe once esphome has given them
# a hook (esphome/esphome#20875); until then they peel. Either way the walker
# must reach a schema that carries the module-level base schema's keys.
_PROBED_OR_DELEGATED_WRAPPERS = [
    ("esphome.components.mapping", "map_schema", "BASE_SCHEMA"),
]

# The subset of delegate sites the shipped catalog nests; every shipped
# nested key must come from the schema the walker resolves for the wrapper.
_NESTED_SITES = [
    ("api", ("encryption",), "esphome.components.api", "_encryption_schema"),
    ("mapping", (), "esphome.components.mapping", "map_schema"),
    ("uart", ("debug",), "esphome.components.uart", "maybe_empty_debug"),
    ("wifi", ("ap",), "esphome.components.wifi", "wifi_network_ap"),
]


@pytest.fixture(autouse=True, scope="module")
def _primed_core() -> None:
    """Prime esphome's CORE so component modules import cleanly."""
    assert _get_esphome_loader() is not None


def _schema_keys(schema: object) -> set[str]:
    schema_dict = _unwrap_schema_to_dict(schema)
    assert schema_dict is not None
    return {str(getattr(key, "schema", key)) for key in schema_dict}


def _walker_schema(module_name: str, wrapper: str) -> object:
    """Resolve a wrapper the way the walker does: its probe answer, else its peel."""
    node = getattr(importlib.import_module(module_name), wrapper)
    schema = _hidden_schema(node)
    return schema if schema is not None else _delegated_schema(node)


@pytest.mark.parametrize(
    ("module_name", "wrapper", "schema_name"),
    [pytest.param(*row, id=f"{row[0].rsplit('.', 1)[-1]}.{row[1]}") for row in _DELEGATE_WRAPPERS],
)
def test_live_delegate_peel_resolves_its_intended_schema(
    module_name: str, wrapper: str, schema_name: str
) -> None:
    """The peel returns exactly the module schema the wrapper delegates to."""
    module = importlib.import_module(module_name)
    assert _delegated_schema(getattr(module, wrapper)) is getattr(module, schema_name)


@pytest.mark.parametrize(
    ("module_name", "wrapper", "schema_name"),
    [
        pytest.param(*row, id=f"{row[0].rsplit('.', 1)[-1]}.{row[1]}")
        for row in _PROBED_OR_DELEGATED_WRAPPERS
    ],
)
def test_live_wrapper_resolves_to_a_schema_holding_the_base_keys(
    module_name: str, wrapper: str, schema_name: str
) -> None:
    """The walker reaches a schema that carries the module-level base schema's keys."""
    module = importlib.import_module(module_name)
    schema = _walker_schema(module_name, wrapper)
    assert schema is not None
    assert _schema_keys(getattr(module, schema_name)) <= _schema_keys(schema)


@pytest.mark.parametrize(
    ("component", "entry_path", "module_name", "wrapper"),
    [pytest.param(*row, id=f"{row[0]}.{'.'.join(row[1]) or '<root>'}") for row in _NESTED_SITES],
)
def test_shipped_nested_keys_come_from_the_delegated_schema(
    component: str, entry_path: tuple[str, ...], module_name: str, wrapper: str
) -> None:
    """Every shipped nested key at a delegate site exists in the schema the walker resolves."""
    body = json.loads((_OUTPUT_BODIES_DIR / f"{component}.json").read_text(encoding="utf-8"))
    entries = body["config_entries"]
    for key in entry_path:
        entries = next(e for e in entries if e["key"] == key).get("config_entries") or []
    shipped = {e["key"] for e in entries}
    assert shipped
    assert shipped <= _schema_keys(_walker_schema(module_name, wrapper))


def test_all_wrapped_delegate_is_not_peeled() -> None:
    """A wrapper referencing a module-level ``cv.All`` delegate is not peeled."""
    namespace = {
        "WRAPPED": cv.All(cv.Schema({cv.Optional("x"): cv.boolean}), lambda value: value),
    }
    exec("def wrapper(value):\n    return WRAPPED(value)", namespace)  # noqa: S102
    assert _delegated_schema(namespace["wrapper"]) is None


def test_schema_builder_config_schema_is_not_peeled() -> None:
    """gsl3670's dynamic CONFIG_SCHEMA builder does not peel to its embedded firmware schema."""
    touchscreen = importlib.import_module("esphome.components.gsl3670.touchscreen")
    assert _delegated_schema(touchscreen._config_schema) is None
