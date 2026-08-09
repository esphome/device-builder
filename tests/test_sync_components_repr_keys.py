"""Callable-repr config-var keys: dropped when acknowledged, fatal otherwise."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import orjson
import pytest

from script.sync_components import (  # type: ignore[import-not-found]
    _AUTOMATIONS_BODIES_DIR,
    _OUTPUT_BODIES_DIR,
    _UNHANDLED_REPR_KEYS,
    _convert_config_vars,
    _fail_on_unhandled_repr_keys,
)

_LEAKED_KEY = "<function validate_parameter_name at 0x7f05234eefc0>"

# Full-string reprs only, so prose mentioning an address ("Defaults to
# `0x7f`") never trips it.
_REPRISH_RE = re.compile(r"^<.+ at 0x[0-9a-f]+>$|^<class '.+'>$")


@pytest.fixture(autouse=True)
def _clean_accumulator() -> Iterator[None]:
    _UNHANDLED_REPR_KEYS.clear()
    yield
    _UNHANDLED_REPR_KEYS.clear()


def _reprish_strings(node: Any, path: str) -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and _REPRISH_RE.match(key):
                yield f"{path}.{key}"
            yield from _reprish_strings(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _reprish_strings(item, f"{path}[{index}]")
    elif isinstance(node, str) and _REPRISH_RE.match(node):
        yield path


def test_shipped_definitions_carry_no_reprs() -> None:
    """No shipped body carries a stringified Python object as key or value."""
    violations = []
    for body_path in [*_OUTPUT_BODIES_DIR.glob("*.json"), *_AUTOMATIONS_BODIES_DIR.rglob("*.json")]:
        body = orjson.loads(body_path.read_bytes())
        violations.extend(f"{body_path.name}: {hit}" for hit in _reprish_strings(body, ""))
    assert not violations


def test_shipped_script_execute_drops_wildcard_entry() -> None:
    """script.execute ships only the id entry; the parameter wildcard is dropped."""
    body = orjson.loads((_AUTOMATIONS_BODIES_DIR / "actions" / "script.execute.json").read_bytes())
    assert {e["key"] for e in body["config_entries"]} == {"id"}


def test_handled_wildcard_key_is_dropped(tmp_path: Path) -> None:
    """An acknowledged callable-repr key produces no entry and no canary row."""
    schema_node = {"config_vars": {_LEAKED_KEY: {"key": "Optional", "templatable": True}}}

    entries = _convert_config_vars(schema_node, tmp_path)

    assert entries == []
    assert set() == _UNHANDLED_REPR_KEYS


def test_normalized_wildcard_placeholder_is_dropped(tmp_path: Path) -> None:
    """The dumper's post-fix ``string`` placeholder (esphome/esphome#18218) also drops."""
    schema_node = {
        "config_vars": {
            "string": {
                "key": "Optional",
                "key_type": "validate_parameter_name",
                "templatable": True,
            }
        }
    }

    entries = _convert_config_vars(schema_node, tmp_path)

    assert entries == []
    assert set() == _UNHANDLED_REPR_KEYS


def test_unknown_wildcard_placeholder_fails_the_sync(tmp_path: Path) -> None:
    """A ``string`` placeholder with an unacknowledged validator aborts before emit."""
    schema_node = {"config_vars": {"string": {"key": "Optional", "key_type": "frob"}}}

    entries = _convert_config_vars(schema_node, tmp_path, component_id="widget")

    assert entries == []
    assert {("widget", "string[key_type=frob]")} == _UNHANDLED_REPR_KEYS
    with pytest.raises(SystemExit, match="widget"):
        _fail_on_unhandled_repr_keys()


@pytest.mark.parametrize(
    "key",
    [
        pytest.param("<function frob at 0x1f>", id="unknown_function"),
        pytest.param("<function <lambda> at 0x1f>", id="lambda"),
        pytest.param("<bound method X.y of <X object at 0x1f>>", id="bound_method"),
    ],
)
def test_unhandled_repr_key_fails_the_sync(key: str, tmp_path: Path) -> None:
    """An unacknowledged repr key is recorded and aborts before emit."""
    schema_node = {"config_vars": {key: {"key": "Optional"}}}

    entries = _convert_config_vars(schema_node, tmp_path, component_id="widget")

    assert entries == []
    assert {("widget", key)} == _UNHANDLED_REPR_KEYS
    with pytest.raises(SystemExit, match="widget"):
        _fail_on_unhandled_repr_keys()
