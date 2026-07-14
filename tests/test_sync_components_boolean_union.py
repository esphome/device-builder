"""Boolean|enum union folding in ``_apply_refined_types``."""

from __future__ import annotations

from pathlib import Path

import pytest

from script.sync_components import (  # type: ignore[import-not-found]
    RefinedType,
    _apply_refined_types,
    _convert_field,
)

# Raw schema-bundle entry for ``zigbee.wipe_on_boot`` — upstream validates
# with ``cv.Any(cv.boolean, cv.one_of("once"))``, so the bundle emits only
# the non-boolean enum half.
_FIXTURE_WIPE_ON_BOOT: dict = {
    "default_with": {"value": "False", "components": ["nrf52"]},
    "key": "Optional",
    "type": "enum",
    "values": {
        "once": {
            "docs": ("Erase data only on first boot after flashing new firmware, then preserve."),
        },
    },
}


@pytest.fixture
def schema_dir(tmp_path: Path) -> Path:
    """Empty dir for ``_convert_field`` (only used for ``extends`` lookups)."""
    return tmp_path


def test_boolean_refinement_folds_into_existing_options(schema_dir: Path) -> None:
    """A boolean|enum union keeps type string and gains true/false options."""
    entry = _convert_field("wipe_on_boot", _FIXTURE_WIPE_ON_BOOT, schema_dir)
    assert entry is not None
    assert entry["type"] == "string"
    _apply_refined_types([entry], {("wipe_on_boot",): RefinedType("boolean")})
    assert entry["type"] == "string"
    assert [option["value"] for option in entry["options"]] == ["true", "false", "once"]
    assert entry["default_value"] == "false"
    assert entry["depends_on_component"] == "nrf52"


def test_boolean_refinement_without_options_still_retypes() -> None:
    entries = [{"key": "sleepy", "type": "string", "options": None, "default_value": False}]
    _apply_refined_types(entries, {("sleepy",): RefinedType("boolean")})
    assert entries[0]["type"] == "boolean"
    assert entries[0]["default_value"] is False


def test_merge_skips_boolean_literals_already_present() -> None:
    entry = {
        "key": "x",
        "type": "string",
        "options": [{"label": "True", "value": "True"}, {"label": "maybe", "value": "maybe"}],
        "default_value": None,
    }
    _apply_refined_types([entry], {("x",): RefinedType("boolean")})
    assert [option["value"] for option in entry["options"]] == ["false", "True", "maybe"]
    assert entry["default_value"] is None


def test_bool_default_adopts_the_surviving_option_casing() -> None:
    entry = {
        "key": "x",
        "type": "string",
        "options": [{"label": "True", "value": "True"}, {"label": "maybe", "value": "maybe"}],
        "default_value": True,
    }
    _apply_refined_types([entry], {("x",): RefinedType("boolean")})
    assert entry["default_value"] == "True"
