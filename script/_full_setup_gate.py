"""
Import-time validation gate: an imported board's full setup must validate.

Each record's featured components are resolved exactly like the create
wizard's "all recommended" flow and run through the real
``esphome.config.load_config`` in a forked worker (ESPHome accumulates
module-global state across validations, so every validation gets a fresh
process — same isolation the slow e2e suite uses). A failing entry is
dropped by mapping the error's structured config path back to the
generated item's ``id``; the record revalidates until clean. Boards left
featureless (or with an unmappable failure) are skipped entirely.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from esphome_device_builder.models import BoardCatalogEntry, ComponentCatalogEntry

_LOGGER = logging.getLogger("sync_esphome_devices")

# Each pass drops at least one entry per failing board; ESPHome often stops
# at the first error per domain, so a page with many same-shaped broken
# entries (one bad expander pin copied eight times) needs a pass per entry.
_MAX_PASSES = 12


def apply_validation_gate(
    records: list[dict[str, Any]], components_index: dict[str, dict[str, Any]]
) -> dict[str, str]:
    """
    Drop featured entries whose generated full setup fails ESPHome validation.

    Mutates *records* in place; returns ``{board_id: skip_reason}`` for
    boards that can't be repaired.
    """
    # Import once in the parent: every forked worker then inherits the
    # clean pre-validation module state instead of re-importing esphome.
    import esphome.config  # noqa: F401

    if "fork" in mp.get_all_start_methods():
        ctx = mp.get_context("fork")
    else:
        # Windows: spawn re-imports esphome per worker (slower) but the
        # fresh-process isolation is the same — never skip validation.
        ctx = mp.get_context("spawn")
    skipped: dict[str, str] = {}
    pending = list(records)
    for _ in range(_MAX_PASSES):
        if not pending:
            break
        processes = min(8, os.cpu_count() or 4, len(pending))
        with ctx.Pool(processes=processes, maxtasksperchild=1) as pool:
            results = pool.map(_validate_record, pending, chunksize=1)
        retry: list[dict[str, Any]] = []
        for record, outcome in zip(pending, results, strict=True):
            if outcome is None or not outcome.drops:
                if outcome is not None and outcome.errors:
                    skipped[record["id"]] = f"full setup fails validation: {outcome.errors[0]}"
                continue
            for local_id, error in outcome.drops:
                _LOGGER.info("%s: dropping %s — %s", record["id"], local_id, error)
            _apply_drops(record, {local_id for local_id, _ in outcome.drops}, components_index)
            if record.get("featured_components"):
                retry.append(record)
            else:
                skipped[record["id"]] = "no featured component survives full-setup validation"
        pending = retry
    for record in pending:
        # Still failing after the pass budget — refuse rather than emit.
        skipped.setdefault(record["id"], "full setup still fails validation after repairs")
    return skipped


def run_esphome_validation(
    board_id: str,
    board: BoardCatalogEntry,
    defaults: list[tuple[ComponentCatalogEntry, dict[str, Any]]],
) -> tuple[str, list[Any]]:
    """
    Generate *board*'s YAML with *defaults* and run real ESPHome validation.

    Returns ``(yaml_text, errors)`` — errors are ``vol.Invalid`` (carrying a
    structured ``.path``) or a single ``EsphomeError``. Shared with the slow
    e2e boards suite; call from a fresh (forked) process only.
    """
    from esphome.config import load_config
    from esphome.core import CORE, EsphomeError

    from esphome_device_builder.helpers.device_yaml import generate_device_yaml

    with tempfile.TemporaryDirectory() as tmp:
        # Inline creds keep the YAML ``!secret``-free so it validates standalone.
        yaml_path = Path(tmp) / f"{board_id}.yaml"
        yaml_text = generate_device_yaml(
            "repro", "Repro", board, ssid="ssid", psk="password", defaults=defaults
        )
        yaml_path.write_text(yaml_text, encoding="utf-8")
        CORE.config_path = yaml_path
        try:
            return yaml_text, list(load_config({}, skip_external_update=True).errors)
        except EsphomeError as err:
            return yaml_text, [err]


@dataclass(slots=True)
class _Outcome:
    """One validation result: nothing set means the record validated clean."""

    # (local id, error text) per entry to drop.
    drops: list[tuple[str, str]] = field(default_factory=list)
    # Board-level failures no single entry can absorb.
    errors: list[str] = field(default_factory=list)


def _validate_record(record: dict[str, Any]) -> _Outcome | None:
    """
    Validate one record's full setup in this (forked) process.

    Returns ``None`` when the gate doesn't apply (pin-conflict boards keep
    partial bundles, so no combined setup exists to validate).
    """
    try:
        return _validate_record_inner(record)
    except Exception as exc:
        return _Outcome(errors=[f"validation crashed: {exc!r}"])


def _validate_record_inner(record: dict[str, Any]) -> _Outcome | None:
    """See :func:`_validate_record`; separated so its crash guard stays total."""
    from esphome_device_builder.controllers.components import _load_body_from_disk
    from esphome_device_builder.definitions import (
        _load_component_multi_conf,
        _load_esphome_config,
        _load_featured_component,
    )
    from esphome_device_builder.models import BoardCatalogEntry
    from script.sync_boards import _has_pin_conflict

    multi_conf = _load_component_multi_conf()
    featured = [
        _load_featured_component(fc, Path(), multi_conf)
        for fc in record.get("featured_components") or []
    ]
    if not featured or _has_pin_conflict(featured):
        return None
    board = BoardCatalogEntry(
        id=record["id"],
        name=record["name"],
        description="",
        manufacturer="",
        esphome=_load_esphome_config(record["esphome"], record["id"]),
        featured_components=featured,
        full_config=True,
    )
    defaults = []
    for fc in featured:
        body = _load_body_from_disk(fc.component_id)
        if body is None:
            return _Outcome(errors=[f"no catalog body for {fc.component_id}"])
        defaults.append(
            (body, {key: p.value for key, p in fc.fields.items() if p.value is not None})
        )
    yaml_text, errors = run_esphome_validation(record["id"], board, defaults)
    if not errors:
        return _Outcome()
    return _map_errors(errors, yaml_text, record)


def _map_errors(errors: list[Any], yaml_text: str, record: dict[str, Any]) -> _Outcome:
    """Map each error's structured config path to the featured entry that produced it."""
    # Function-level import: this module loads while sync_esphome_devices is
    # still importing it, so a top-level import would be circular.
    from script.sync_esphome_devices import _safe_load_yaml

    # The generated YAML can carry ESPHome-only tags (``!lambda``) that the
    # plain safe loader rejects.
    data = _safe_load_yaml(yaml_text) or {}
    entries = record.get("featured_components") or []
    local_ids = {entry["id"] for entry in entries}
    drops: list[tuple[str, str]] = []
    seen: set[str] = set()
    for error in errors:
        path = list(getattr(error, "path", None) or [])
        local_id = _entry_for_path(path, data, entries, local_ids)
        if local_id is None:
            # An error we can't pin on one entry poisons the whole board.
            return _Outcome(errors=[str(error)])
        if local_id not in seen:
            seen.add(local_id)
            drops.append((local_id, str(error)))
    return _Outcome(drops=drops)


def _entry_for_path(
    path: list[Any],
    data: dict[str, Any],
    entries: list[dict[str, Any]],
    local_ids: set[str],
) -> str | None:
    """Resolve one structured config path to a featured local id."""
    if not path or not isinstance(path[0], str):
        return None
    domain = path[0]
    block = data.get(domain)
    item: Any = None
    if len(path) > 1 and isinstance(path[1], int) and isinstance(block, list):
        item = block[path[1]] if path[1] < len(block) else None
    elif isinstance(block, dict):
        item = block
    if isinstance(item, dict):
        item_id = item.get("id")
        if isinstance(item_id, str) and item_id in local_ids:
            return item_id
    # Mapping-style hubs generate without their local id; fall back to the
    # sole featured entry of the domain.
    matches = [
        entry["id"]
        for entry in entries
        if entry["component_id"] == domain or entry["component_id"].startswith(f"{domain}.")
    ]
    return matches[0] if len(matches) == 1 else None


def _apply_drops(
    record: dict[str, Any], drop_ids: set[str], components_index: dict[str, dict[str, Any]]
) -> None:
    """Remove *drop_ids* from the record's featured entries, bundles, requires, and pins."""
    dropped = [
        entry for entry in record.get("featured_components") or [] if entry["id"] in drop_ids
    ]
    record["featured_components"] = [
        entry for entry in record.get("featured_components") or [] if entry["id"] not in drop_ids
    ]
    for entry in record["featured_components"]:
        requires = [ref for ref in entry.get("requires") or [] if ref not in drop_ids]
        if requires:
            entry["requires"] = requires
        elif "requires" in entry:
            del entry["requires"]
    bundles = [
        bundle
        for bundle in record.get("featured_bundles") or []
        if [member for member in bundle["component_ids"] if member not in drop_ids]
    ]
    for bundle in bundles:
        bundle["component_ids"] = [
            member for member in bundle["component_ids"] if member not in drop_ids
        ]
    if bundles:
        record["featured_bundles"] = bundles
    elif "featured_bundles" in record:
        del record["featured_bundles"]
    _prune_dropped_pins(record, dropped, components_index)


def _prune_dropped_pins(
    record: dict[str, Any],
    dropped: list[dict[str, Any]],
    components_index: dict[str, dict[str, Any]],
) -> None:
    """
    Drop pins owned by removed entries; relabel GPIOs a survivor still locks.

    ``occupied_by`` comes from ``_occupancy_label`` (cleaned upstream
    name/id, else the component id), so match every label a dropped entry
    can have produced — but a shared GPIO must stay declared or a surviving
    entry's locked pin loses its board declaration.
    """
    labels: set[str] = set()
    for entry in dropped:
        labels.add(entry["id"])
        labels.add(entry["component_id"])
        fields = entry.get("fields") or {}
        raw_id = fields.get("id")
        for label in (
            entry.get("name"),
            fields.get("name"),
            raw_id.get("value") if isinstance(raw_id, dict) else raw_id,
        ):
            if isinstance(label, str):
                labels.add(label)
    claimed = _surviving_gpio_labels(record["featured_components"], components_index)
    pins = []
    for pin in record.get("pins") or []:
        if pin.get("occupied_by") not in labels:
            pins.append(pin)
        elif pin.get("gpio") in claimed:
            pins.append({**pin, "occupied_by": claimed[pin["gpio"]]})
    if pins:
        record["pins"] = pins
    elif "pins" in record:
        del record["pins"]


def _surviving_gpio_labels(
    entries: list[dict[str, Any]], components_index: dict[str, dict[str, Any]]
) -> dict[int, str]:
    """
    Map each GPIO a surviving entry locks to that entry's display label.

    Pin fields are detected by the catalog's ``type: "pin"`` (``sda``, ``clk``)
    plus the pin-group name pattern (``data_pins``), matching how extraction
    recorded occupancy in the first place.
    """
    from script.sync_esphome_devices import _PIN_TREE_FIELD_RE

    claimed: dict[int, str] = {}
    for entry in entries:
        component = components_index.get(entry["component_id"]) or {}
        pin_keys = {
            ce["key"]
            for ce in component.get("config_entries") or []
            if ce.get("type") == "pin" and isinstance(ce.get("key"), str)
        }
        fields = entry.get("fields") or {}
        name = fields.get("name")
        label = name if isinstance(name, str) else entry["id"]
        for key, preset in fields.items():
            if key not in pin_keys and not _PIN_TREE_FIELD_RE.search(key):
                continue
            value = preset.get("value") if isinstance(preset, dict) else preset
            _walk_gpio_leaves(value, label, claimed)
    return claimed


def _walk_gpio_leaves(value: Any, label: str, claimed: dict[int, str]) -> None:
    """Record every board-GPIO leaf of a pin value (scalar, dict, or pin-group tree)."""
    from script.sync_esphome_devices import _expander_keys, _gpio_number

    if isinstance(value, dict):
        if _expander_keys(value):
            # An expander channel's ``number`` is not a board GPIO.
            return
        if "number" not in value:
            for item in value.values():
                _walk_gpio_leaves(item, label, claimed)
            return
        value = value["number"]
    if isinstance(value, list):
        for item in value:
            _walk_gpio_leaves(item, label, claimed)
        return
    gpio = _gpio_number(value)
    if gpio is not None:
        claimed.setdefault(gpio, label)
