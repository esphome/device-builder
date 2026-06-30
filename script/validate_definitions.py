#!/usr/bin/env python3
"""Validate board and component definition manifests.

Checks that all manifest.yaml files in the definitions directory
have the required fields and valid structure.

Used as a pre-commit hook and in CI.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

try:
    import jsonschema

    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Imported from the stdlib-only constants module so this script stays light.
from esphome_device_builder.constants import BOARD_PIN_KEYS, BUS_CATEGORIES  # noqa: E402

DEFINITIONS_DIR = _REPO_ROOT / "esphome_device_builder" / "definitions"
SCHEMAS_DIR = DEFINITIONS_DIR / "schemas"
COMPONENTS_INDEX_JSON = DEFINITIONS_DIR / "components.index.json"
COMPONENTS_BODIES_DIR = DEFINITIONS_DIR / "components"

# Categories excluded from featured-component eligibility — these belong in
# the dedicated "Add core configuration" dialog, not in board recommendations.
_FEATURED_EXCLUDED_CATEGORIES = {"core", "ota", "time", "update"}

# Network components offered as board "suggested hardware" despite their
# ``core`` category — auto-pulled in place of wifi: when a board has onboard
# wired/Thread networking. Runtime counterpart is
# ``NETWORK_PROVIDER_COMPONENT_IDS`` in helpers/device_yaml/_generation.py;
# keep both in sync when adding a provider (that module pulls the heavy helper
# layer, so it's mirrored here rather than imported).
_FEATURED_CATEGORY_EXCEPTIONS = {"ethernet"}

# Required shape for featured-component ids: lowercase letters, digits, and
# underscores only, starting with a letter. Mirrors what ESPHome accepts
# as a valid identifier and what the sync script's auto-id format produces.
_FEATURED_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

# ``(board_id, bus)`` pairs whose source can't yet express a lift-able bus, so a
# featured leaf's dependency on that bus is knowingly unsatisfied (the full-setup
# config won't compile) pending a source-level fix. Keyed on the specific bus, not
# the whole board, so a *different* unsatisfied bus on the same board still fails.
# This is an allow list, not a silent skip: removing a pair must be paired with a
# fix in script/sync_esphome_devices.py.
_UNSATISFIED_BUS_ALLOW_LIST = frozenset(
    {
        ("kincony_ag8", "uart"),  # 2 id-less switch.uart consumers; bus ref not recovered
        ("kincony_kc868_e8t", "uart"),  # 2 id-less bl0939 consumers; bus ref not recovered
        ("kincony_mb", "i2c"),  # 16 id-less ina226 consumers; bus ref not recovered
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
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
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
    Index the component catalog for featured-component cross-checks.

    Joins ``components.index.json`` with each per-id body file so
    every entry carries the ``config_entries`` tree the featured-
    field validation needs. Returns ``None`` when the catalog is
    missing — featured-component cross-validation is skipped
    (schema-only) and a warning is printed so contributors know
    to run ``script/sync_components.py`` first.
    """
    if not COMPONENTS_INDEX_JSON.exists():
        print(
            f"WARNING: {COMPONENTS_INDEX_JSON} not found — skipping featured-component "
            "cross-validation. Run script/sync_components.py first.",
            file=sys.stderr,
        )
        return None
    raw = json.loads(COMPONENTS_INDEX_JSON.read_text(encoding="utf-8"))
    by_id: dict[str, dict] = {}
    for comp in raw.get("components", []):
        cid = comp.get("id")
        if not cid:
            continue
        body_path = COMPONENTS_BODIES_DIR / f"{cid}.json"
        if body_path.is_file():
            body = json.loads(body_path.read_text(encoding="utf-8"))
            by_id[cid] = {**comp, **body}
        else:
            by_id[cid] = comp
    return by_id


def _validate_featured(  # noqa: C901
    board_id: str,
    data: dict,
    pins_by_gpio: dict[int, dict],
    components_index: dict | None,
    is_imported: bool = False,
) -> list[str]:
    """Validate featured_components / featured_bundles / default_components cross-references."""
    errors: list[str] = []
    featured = data.get("featured_components") or []
    bundles = data.get("featured_bundles") or []
    defaults = data.get("default_components") or []
    if not featured and not bundles and not defaults:
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

    errors.extend(_validate_default_components(board_id, defaults, seen_fc_ids, components_index))
    errors.extend(
        _validate_featured_dependencies(board_id, featured, components_index, is_imported, defaults)
    )
    return errors


def _validate_default_components(
    board_id: str,
    defaults: list,
    seen_fc_ids: set[str],
    components_index: dict | None,
) -> list[str]:
    """Cross-check each ``default_components`` ref against featured + catalog ids."""
    if not defaults or components_index is None:
        return []
    catalog_ids = set(components_index)
    out: list[str] = []
    for idx, entry in enumerate(defaults):
        if isinstance(entry, str):
            ref = entry
        elif isinstance(entry, dict):
            ref = entry.get("id")
            if not isinstance(ref, str):
                out.append(f"{board_id}.default_components[{idx}]: missing 'id' field")
                continue
        else:
            continue
        if ref in seen_fc_ids or ref in catalog_ids:
            continue
        out.append(
            f"{board_id}.default_components[{idx}]: '{ref}' does not match any "
            f"featured_components[].id or known component_id"
        )
    return out


def _is_bus_dep(dep: str, components_index: dict) -> bool:
    """Whether *dep* names a bus, mapping- (top-level, category bus) or platform-style."""
    component = components_index.get(dep)
    if component is not None:
        return component.get("category") in BUS_CATEGORIES
    return dep in BUS_CATEGORIES


def _ref_ids(entries: list) -> set[str]:
    """Component ids/refs named by a featured or default-components list."""
    ids: set[str] = set()
    for entry in entries:
        if isinstance(entry, str):
            ids.add(entry)
        elif isinstance(entry, dict) and isinstance(entry.get("component_id"), str):
            ids.add(entry["component_id"])
        elif isinstance(entry, dict) and isinstance(entry.get("id"), str):
            ids.add(entry["id"])
    return ids


def _validate_featured_dependencies(
    board_id: str,
    featured: list,
    components_index: dict | None,
    is_imported: bool,
    defaults: list | None = None,
) -> list[str]:
    """
    Flag a featured leaf whose bus dependency no component on the board provides.

    An imported board ships its featured components as a complete config, so a
    leaf binding a bus (i2c/spi/uart/modbus/one_wire/canbus) by catalog dependency
    won't compile unless the bus is provided too (lifted by the sync script into
    featured or default components). Only imported boards are checked; a
    ``(board, bus)`` pair in the allow list — a known source-level gap — is waived
    while any *other* unsatisfied bus on the same board still fails.
    """
    if not is_imported or components_index is None:
        return []
    present = _ref_ids(featured) | _ref_ids(defaults or [])
    present_domains = {cid.split(".")[0] for cid in present}
    out: list[str] = []
    for idx, entry in enumerate(featured):
        if not isinstance(entry, dict):
            continue
        cid = entry.get("component_id")
        # Only platform leaves (``<domain>.<platform>`` — sensors, displays,
        # touchscreens) bind a bus unconditionally; their bus is their sole
        # connection. Bare top-level components are buses themselves (no bus dep)
        # or dual-mode hubs whose bus dependency is conditional (``sn74hc595``
        # bit-bangs over GPIO *or* runs on spi), so the catalog ``dependencies``
        # over-declares the bus and checking them here would false-positive.
        if not isinstance(cid, str) or "." not in cid:
            continue
        component = components_index.get(cid)
        if not component:
            continue
        for dep in component.get("dependencies") or []:
            if not isinstance(dep, str) or not _is_bus_dep(dep, components_index):
                continue
            if dep in present or dep in present_domains:
                continue
            if (board_id, dep) in _UNSATISFIED_BUS_ALLOW_LIST:
                continue
            out.append(
                f"{board_id}.featured_components[{idx}]({entry.get('id')}): depends on bus "
                f"'{dep}' but no featured component provides it; the full-setup config won't "
                f"compile. Lift the bus in script/sync_esphome_devices.py, or add "
                f"({board_id!r}, {dep!r}) to _UNSATISFIED_BUS_ALLOW_LIST with the source reason."
            )
    return out


def _validate_featured_component(  # noqa: C901
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
        errors.append(f"{path}: component_id '{component_id}' not found in components.index.json")
        return errors

    component = components_index[component_id]
    if (
        component.get("category") in _FEATURED_EXCLUDED_CATEGORIES
        and component_id not in _FEATURED_CATEGORY_EXCEPTIONS
    ):
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

    for fkey, fval in (entry.get("fields") or {}).items():
        if fkey not in entries_by_key:
            # ``id`` is universal across every component; every other field —
            # including ``name`` — must be a declared config entry, mirroring the
            # importer, which injects ``name`` only when the schema declares it.
            if fkey == "id":
                continue
            errors.append(f"{path}.fields.{fkey}: not a config_entry on {component_id}")
            continue
        ce = entries_by_key[fkey]
        errors.extend(_validate_field_preset(path, fkey, fval, ce, pins_by_gpio, is_imported))

    return errors


def _is_expander_pin(raw: object) -> bool:
    """Whether *raw* is a long-form pin sitting on an I/O-expander hub."""
    return isinstance(raw, dict) and bool(raw.keys() - BOARD_PIN_KEYS)


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
            if _is_expander_pin(raw):
                # The pin sits on an I/O expander; its ``number`` is an
                # expander channel, not a board GPIO, so it isn't checked
                # against the board pins.
                continue
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
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return [f"{comp_id}: invalid YAML: {exc}"]

    if not isinstance(data, dict):
        return [f"{comp_id}: manifest is not a YAML mapping"]

    # JSON Schema validation
    errors.extend(_validate_against_schema(data, _COMPONENT_SCHEMA, comp_id))
    if errors:
        return errors

    return errors


# Browser-like UA: some vendor CDNs 403 the default urllib agent.
_IMAGE_USER_AGENT = "Mozilla/5.0 (compatible; esphome-device-builder-linkcheck/1.0)"
_IMAGE_FETCH_TIMEOUT = 15
_IMAGE_MAX_WORKERS = 32


def check_board_images(
    boards_dir: Path,
    fetch: Callable[[str], int] | None = None,
    max_workers: int = _IMAGE_MAX_WORKERS,
) -> list[str]:
    """
    Verify every board manifest ``images:`` URL is reachable (HTTP 2xx).

    Network-gated; returns one error line per unreachable URL. ``fetch``
    is injectable so tests classify statuses without real I/O.
    """
    if fetch is None:
        fetch = _fetch_image_status
    url_to_boards = _collect_board_image_urls(boards_dir)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        statuses = dict(
            zip(
                url_to_boards,
                pool.map(lambda url: _safe_fetch(url, fetch), url_to_boards),
                strict=True,
            )
        )
    errors: list[str] = []
    for url, status in statuses.items():
        if isinstance(status, int) and 200 <= status < 300:
            continue
        errors.extend(f"{board_id}: image {url} -> {status}" for board_id in url_to_boards[url])
    return errors


def _collect_board_image_urls(boards_dir: Path) -> dict[str, list[str]]:
    """Map each unique http(s) ``images:`` URL to the board ids referencing it."""
    urls: dict[str, list[str]] = {}
    for manifest in sorted(boards_dir.glob("*/manifest.yaml")):
        try:
            data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue
        if not isinstance(data, dict):
            continue
        board_id = manifest.parent.name
        for img in data.get("images") or []:
            if isinstance(img, str) and img.startswith(("http://", "https://")):
                urls.setdefault(img, []).append(board_id)
    return urls


def _safe_fetch(url: str, fetch: Callable[[str], int]) -> int | str:
    """Run *fetch*, turning any network failure into a reportable string."""
    try:
        return fetch(url)
    except Exception as exc:
        return f"error: {exc}"


def _fetch_image_status(url: str) -> int:
    """GET *url* and return its HTTP status (4xx/5xx returned, not raised)."""
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": _IMAGE_USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=_IMAGE_FETCH_TIMEOUT) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def main() -> int:
    """Validate all definitions. Returns 0 on success, 1 on errors."""
    parser = argparse.ArgumentParser(description="Validate definition manifests.")
    parser.add_argument(
        "--check-images",
        action="store_true",
        help="Also verify board manifest images: URLs resolve (network; opt-in).",
    )
    args = parser.parse_args()

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

    if args.check_images:
        all_errors.extend(check_board_images(boards_dir))

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
