#!/usr/bin/env python3
"""Validate board and component definition manifests.

Checks that all manifest.yaml files in the definitions directory
have the required fields and valid structure.

Used as a pre-commit hook and in CI.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

try:
    import jsonschema

    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False

DEFINITIONS_DIR = Path(__file__).resolve().parent.parent / "esphome_device_builder" / "definitions"
SCHEMAS_DIR = DEFINITIONS_DIR / "schemas"
COMPONENTS_JSON = DEFINITIONS_DIR / "components.json"

# Categories excluded from featured-component eligibility — these belong in
# the dedicated "Add core configuration" dialog, not in board recommendations.
_FEATURED_EXCLUDED_CATEGORIES = {"core", "ota", "time", "update"}

# Required shape for featured-component ids: lowercase letters, digits, and
# underscores only, starting with a letter. Mirrors what ESPHome accepts
# as a valid identifier and what the sync script's auto-id format produces.
_FEATURED_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

# Categories whose ESPHome schema accepts a top-level ``name:`` field
# (entity-base universals). Used to gate the ``fields.name`` exception
# below — without this, a manifest could attach ``fields.name`` to an
# ``output.*`` component and still validate, then later compile-fail
# because ``output:`` doesn't carry a ``name`` field.
_HA_ENTITY_CATEGORIES: frozenset[str] = frozenset(
    {
        "alarm_control_panel",
        "binary_sensor",
        "button",
        "camera",
        "climate",
        "cover",
        "datetime",
        "display",
        "event",
        "fan",
        "light",
        "lock",
        "media_player",
        "microphone",
        "number",
        "select",
        "sensor",
        "speaker",
        "switch",
        "text",
        "text_sensor",
        "touchscreen",
        "update",
        "valve",
    }
)

# Pin features the board manifest can declare (mirrors the JSON Schema enum
# in board.schema.json). Components.json sometimes carries pin_features
# values like "input" / "output" that the board side doesn't model — we
# only enforce intersections with this set during cross-validation.
_BOARD_PIN_FEATURES = {
    "adc",
    "dac",
    "touch",
    "pwm",
    "i2c_sda",
    "i2c_scl",
    "spi_mosi",
    "spi_miso",
    "spi_clk",
    "spi_cs",
    "uart_tx",
    "uart_rx",
    "usb_dp",
    "usb_dm",
    "rgb_led",
    "jtag",
    "strapping",
    "input_only",
    "boot_button",
}

# Load JSON schemas if jsonschema is available
_BOARD_SCHEMA: dict | None = None
_COMPONENT_SCHEMA: dict | None = None

if HAS_JSONSCHEMA:
    _board_schema_path = SCHEMAS_DIR / "board.schema.json"
    if _board_schema_path.exists():
        _BOARD_SCHEMA = json.loads(_board_schema_path.read_text())

    _component_schema_path = SCHEMAS_DIR / "component.schema.json"
    if _component_schema_path.exists():
        _COMPONENT_SCHEMA = json.loads(_component_schema_path.read_text())


def _validate_against_schema(data: dict, schema: dict | None, item_id: str) -> list[str]:
    """Validate data against a JSON schema. Returns error messages."""
    if not HAS_JSONSCHEMA or schema is None:
        return []
    errors: list[str] = []
    for error in jsonschema.Draft7Validator(schema).iter_errors(data):
        path = ".".join(str(p) for p in error.absolute_path) or "(root)"
        errors.append(f"{item_id}: schema error at {path}: {error.message}")
    return errors


def validate_board(manifest: Path, components_index: dict | None = None) -> list[str]:
    """
    Validate a board manifest. Returns list of error messages.

    *components_index* is the dict returned by :func:`_build_components_index`;
    when provided, featured-component cross-references are validated against
    the live component catalog.
    """
    errors: list[str] = []
    board_id = manifest.parent.name

    try:
        data = yaml.safe_load(manifest.read_text())
    except yaml.YAMLError as exc:
        return [f"{board_id}: invalid YAML: {exc}"]

    if not isinstance(data, dict):
        return [f"{board_id}: manifest is not a YAML mapping"]

    # JSON Schema validation
    errors.extend(_validate_against_schema(data, _BOARD_SCHEMA, board_id))
    if errors:
        return errors  # schema errors are comprehensive, skip manual checks

    # Extra checks beyond schema
    # ID must match folder name
    if data.get("id") and data["id"] != board_id:
        errors.append(f"{board_id}: id '{data['id']}' does not match folder name")

    # Duplicate GPIO check (schema can't do cross-item uniqueness)
    pins = data.get("pins", [])
    pins_by_gpio: dict[int, dict] = {}
    if isinstance(pins, list):
        seen_gpios: set[int] = set()
        for pin in pins:
            if isinstance(pin, dict) and (gpio := pin.get("gpio")) is not None:
                if gpio in seen_gpios:
                    errors.append(f"{board_id}: duplicate gpio {gpio}")
                seen_gpios.add(gpio)
                pins_by_gpio[gpio] = pin

    # Imported boards (source.type set) carry only synthesized pin
    # entries with empty ``features`` — we don't have a per-chip pin-
    # feature DB to populate them. Skip the per-pin feature
    # intersection check for these; the rest of featured-component
    # validation (component_id present, fields key match,
    # GPIO declared) still runs.
    is_imported = isinstance(data.get("source"), dict) and bool(data["source"].get("type"))

    # Featured components & bundles — cross-catalog validation against
    # the loaded component index when available.
    errors.extend(_validate_featured(board_id, data, pins_by_gpio, components_index, is_imported))

    return errors


def _build_components_index() -> dict | None:
    """
    Index ``components.json`` for featured-component cross-checks.

    Returns ``None`` when the file is missing — featured-component
    cross-validation is skipped (schema-only) and a warning is printed
    so contributors know to run ``script/sync_components.py`` first.
    """
    if not COMPONENTS_JSON.exists():
        print(
            f"WARNING: {COMPONENTS_JSON} not found — skipping featured-component "
            "cross-validation. Run script/sync_components.py first.",
            file=sys.stderr,
        )
        return None
    raw = json.loads(COMPONENTS_JSON.read_text(encoding="utf-8"))
    by_id: dict[str, dict] = {}
    for comp in raw.get("components", []):
        cid = comp.get("id")
        if cid:
            by_id[cid] = comp
    return by_id


def _validate_featured(
    board_id: str,
    data: dict,
    pins_by_gpio: dict[int, dict],
    components_index: dict | None,
    is_imported: bool = False,
) -> list[str]:
    """Validate featured_components / featured_bundles cross-references."""
    errors: list[str] = []
    featured = data.get("featured_components") or []
    bundles = data.get("featured_bundles") or []
    if not featured and not bundles:
        return errors

    # Local id uniqueness within featured_components and featured_bundles.
    seen_fc_ids: set[str] = set()
    for idx, entry in enumerate(featured):
        if not isinstance(entry, dict):
            continue
        fc_id = entry.get("id")
        if not isinstance(fc_id, str):
            continue
        if fc_id in seen_fc_ids:
            errors.append(f"{board_id}.featured_components[{idx}]: duplicate id '{fc_id}'")
        seen_fc_ids.add(fc_id)

        errors.extend(
            _validate_featured_component(
                board_id, idx, entry, pins_by_gpio, components_index, is_imported
            )
        )

    seen_bundle_ids: set[str] = set()
    for idx, bundle in enumerate(bundles):
        if not isinstance(bundle, dict):
            continue
        b_id = bundle.get("id")
        if isinstance(b_id, str):
            if b_id in seen_bundle_ids:
                errors.append(f"{board_id}.featured_bundles[{idx}]: duplicate id '{b_id}'")
            seen_bundle_ids.add(b_id)
            if not _FEATURED_ID_PATTERN.fullmatch(b_id):
                errors.append(
                    f"{board_id}.featured_bundles[{idx}]({b_id}): id '{b_id}' must match "
                    f"{_FEATURED_ID_PATTERN.pattern} (lowercase letters, digits, "
                    "underscores; no hyphens)"
                )
        errors.extend(
            f"{board_id}.featured_bundles[{idx}].component_ids: "
            f"'{cid}' does not match any featured_components[].id"
            for cid in bundle.get("component_ids", []) or []
            if cid not in seen_fc_ids
        )
    return errors


def _validate_featured_component(
    board_id: str,
    idx: int,
    entry: dict,
    pins_by_gpio: dict[int, dict],
    components_index: dict | None,
    is_imported: bool = False,
) -> list[str]:
    """Validate a single featured_components[i] entry against the catalog."""
    errors: list[str] = []
    fc_id = entry.get("id", f"#{idx}")
    component_id = entry.get("component_id")
    path = f"{board_id}.featured_components[{idx}]({fc_id})"

    # Shape + collision checks on the local id. Run before the
    # components_index gate so they catch bad ids even when the catalog
    # isn't loaded.
    if isinstance(fc_id, str) and entry.get("id") is not None:
        if not _FEATURED_ID_PATTERN.fullmatch(fc_id):
            errors.append(
                f"{path}: id '{fc_id}' must match {_FEATURED_ID_PATTERN.pattern} "
                "(lowercase letters, digits, underscores; no hyphens)"
            )
        if isinstance(component_id, str):
            # Collision check: an id equal to the component_id's domain
            # (the bit before the dot, or the whole string for single-
            # domain ids like ``i2c``) clashes with the ESPHome block
            # name (``output:``, ``i2c:``). Pick a descriptive role,
            # e.g. ``output_relay`` instead of ``output``.
            domain = component_id.split(".", 1)[0]
            if fc_id == domain:
                errors.append(
                    f"{path}: id '{fc_id}' clashes with domain '{domain}' of "
                    f"component_id '{component_id}'; use a descriptive name "
                    f"like '{domain}_<role>' instead"
                )

    if components_index is None:
        # Without a component index we can only sanity-check the local
        # shape; cross-references stay unverified.
        return errors

    if component_id not in components_index:
        errors.append(f"{path}: component_id '{component_id}' not found in components.json")
        return errors

    component = components_index[component_id]
    if component.get("category") in _FEATURED_EXCLUDED_CATEGORIES:
        errors.append(
            f"{path}: component_id '{component_id}' has excluded category "
            f"'{component.get('category')}'; featured components must be "
            "regular catalog entries"
        )

    # Map config-entry keys → entry for fast lookup of pin_features / type.
    entries_by_key: dict[str, dict] = {}
    for ce in component.get("config_entries", []) or []:
        key = ce.get("key")
        if isinstance(key, str):
            entries_by_key[key] = ce

    component_category = component.get("category")
    for fkey, fval in (entry.get("fields") or {}).items():
        if fkey not in entries_by_key:
            if _is_entity_base_universal(fkey, component_category):
                continue
            errors.append(f"{path}.fields.{fkey}: not a config_entry on {component_id}")
            continue
        ce = entries_by_key[fkey]
        errors.extend(_validate_field_preset(path, fkey, fval, ce, pins_by_gpio, is_imported))

    return errors


def _is_entity_base_universal(fkey: str, category: str | None) -> bool:
    """
    Return ``True`` for fields ESPHome accepts beyond the catalog schema.

    ``id`` is universal across every component. ``name`` is part of
    ENTITY_BASE_SCHEMA, inherited by every HA-entity-domain platform —
    but the schema sync misses it for several entity components
    (binary_sensor.gpio, sensor.aht10, ...), so a manifest setting
    ``fields.name`` on those would otherwise trip the unknown-key gate.
    """
    if fkey == "id":
        return True
    return fkey == "name" and category in _HA_ENTITY_CATEGORIES


def _validate_field_preset(
    path: str,
    fkey: str,
    fval: object,
    ce: dict,
    pins_by_gpio: dict[int, dict],
    is_imported: bool = False,
) -> list[str]:
    """Validate a single field preset against its config-entry constraints."""
    errors: list[str] = []
    locked, value, suggestions = _unpack_field_preset(fval)

    if locked and suggestions is not None:
        errors.append(f"{path}.fields.{fkey}: cannot set both 'locked' and 'suggestions'")

    if ce.get("type") == "pin":
        # Limit the constraint to features both sides actually model.
        # Component-side ``pin_features`` like ``input`` / ``output``
        # don't appear in the board-pin enum — skip them rather than
        # fail every plain-GPIO recommendation.
        required_features = {f for f in (ce.get("pin_features") or []) if f in _BOARD_PIN_FEATURES}
        for raw in _pin_values_to_check(value, suggestions):
            gpio = _extract_gpio(raw)
            if gpio is None:
                # Best-effort: rich pin specs without a recognisable
                # ``number`` (e.g. lambdas) are skipped rather than failed.
                continue
            pin = pins_by_gpio.get(gpio)
            if pin is None:
                errors.append(f"{path}.fields.{fkey}: GPIO {gpio} not declared in pins")
                continue
            if is_imported:
                # Imported boards have synthesized pin entries with no
                # features filled in — skip the intersection check.
                # Pin-declared check above still runs.
                continue
            pin_features = set(pin.get("features") or [])
            missing = required_features - pin_features
            if missing:
                errors.append(
                    f"{path}.fields.{fkey}: GPIO {gpio} is missing required "
                    f"pin features {sorted(missing)}"
                )
    return errors


def _extract_gpio(raw: object) -> int | None:
    """
    Pull the GPIO number out of a pin reference.

    Pins can be expressed two ways in ESPHome YAML — bare integer
    (``pin: 12``) or rich mapping (``pin: { number: 0, mode: ..., inverted: ... }``).
    Returns ``None`` for anything else (lambdas, strings, missing
    ``number``) so the caller treats it as un-validatable.
    """
    if isinstance(raw, bool):  # bool is an int subclass — exclude it
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, dict):
        number = raw.get("number")
        if isinstance(number, int) and not isinstance(number, bool):
            return number
    return None


def _unpack_field_preset(raw: object) -> tuple[bool, object, list | None]:
    """Return ``(locked, value, suggestions)`` from any of the accepted shapes."""
    if isinstance(raw, dict):
        # Schema validation already rejects non-list ``suggestions`` with a
        # readable error; this defensive check keeps the validator from
        # crashing when run without jsonschema installed.
        raw_suggestions = raw.get("suggestions")
        suggestions = list(raw_suggestions) if isinstance(raw_suggestions, list) else None
        return bool(raw.get("locked", False)), raw.get("value"), suggestions
    return False, raw, None


def _pin_values_to_check(value: object, suggestions: list | None) -> list[object]:
    """Collect every concrete pin reference in a preset for GPIO validation."""
    out: list[object] = []
    if value is not None:
        out.append(value)
    if suggestions:
        out.extend(suggestions)
    return out


def validate_component(manifest: Path) -> list[str]:
    """Validate a component manifest. Returns list of error messages."""
    errors: list[str] = []
    comp_id = manifest.parent.name

    try:
        data = yaml.safe_load(manifest.read_text())
    except yaml.YAMLError as exc:
        return [f"{comp_id}: invalid YAML: {exc}"]

    if not isinstance(data, dict):
        return [f"{comp_id}: manifest is not a YAML mapping"]

    # JSON Schema validation
    errors.extend(_validate_against_schema(data, _COMPONENT_SCHEMA, comp_id))
    if errors:
        return errors

    return errors


def main() -> int:
    """Validate all definitions. Returns 0 on success, 1 on errors."""
    all_errors: list[str] = []

    components_index = _build_components_index()

    # Validate boards
    boards_dir = DEFINITIONS_DIR / "boards"
    for manifest in sorted(boards_dir.glob("*/manifest.yaml")):
        all_errors.extend(validate_board(manifest, components_index))

    # Validate components
    components_dir = DEFINITIONS_DIR / "components"
    for manifest in sorted(components_dir.glob("*/manifest.yaml")):
        all_errors.extend(validate_component(manifest))

    if all_errors:
        for error in all_errors:
            print(f"ERROR: {error}", file=sys.stderr)
        print(f"\n{len(all_errors)} error(s) found", file=sys.stderr)
        return 1

    board_count = len(list(boards_dir.glob("*/manifest.yaml")))
    comp_count = len(list(components_dir.glob("*/manifest.yaml")))
    print(f"OK: {board_count} boards, {comp_count} components validated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
