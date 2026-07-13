"""
Import-time validation gate: an imported board's full setup must validate.

Each record's featured components are resolved exactly like the create
wizard's "all recommended" flow and run through the real
``esphome.config.load_config`` in a forked worker (ESPHome accumulates
module-global state across validations, so every validation gets a fresh
process — same isolation the slow e2e suite uses). A failing entry is
dropped by mapping the error's ``@ data['<domain>'][i]`` path back to the
generated item's ``id``; the record revalidates until clean. Boards left
featureless (or with an unmappable failure) are skipped entirely.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml

_LOGGER = logging.getLogger("sync_esphome_devices")

_ERROR_PATH_RE = re.compile(r"@ data\['([a-z_0-9]+)'\](?:\[(\d+)\])?")
# Each pass drops at least one entry per failing board; ESPHome often stops
# at the first error per domain, so a page with many same-shaped broken
# entries (one bad expander pin copied eight times) needs a pass per entry.
_MAX_PASSES = 12


def apply_validation_gate(records: list[dict[str, Any]]) -> dict[str, str]:
    """
    Drop featured entries whose generated full setup fails ESPHome validation.

    Mutates *records* in place; returns ``{board_id: skip_reason}`` for
    boards that can't be repaired.
    """
    if "fork" not in mp.get_all_start_methods():
        _LOGGER.warning("Skipping full-setup validation: no fork start method")
        return {}
    ctx = mp.get_context("fork")
    skipped: dict[str, str] = {}
    pending = list(records)
    for _ in range(_MAX_PASSES):
        if not pending:
            break
        with ctx.Pool(processes=min(8, os.cpu_count() or 4), maxtasksperchild=1) as pool:
            results = pool.map(_validate_record, pending, chunksize=1)
        retry: list[dict[str, Any]] = []
        for record, outcome in zip(pending, results, strict=True):
            if outcome is None or not outcome.drop_ids:
                if outcome is not None and outcome.errors:
                    skipped[record["id"]] = f"full setup fails validation: {outcome.errors[0]}"
                continue
            for local_id, error in zip(outcome.drop_ids, outcome.drop_errors, strict=True):
                _LOGGER.info("%s: dropping %s — %s", record["id"], local_id, error)
            _apply_drops(record, set(outcome.drop_ids))
            if record.get("featured_components"):
                retry.append(record)
            else:
                skipped[record["id"]] = "no featured component survives full-setup validation"
        pending = retry
    for record in pending:
        # Still failing after the pass budget — refuse rather than emit.
        skipped.setdefault(record["id"], "full setup still fails validation after repairs")
    return skipped


class _Outcome:
    """One validation result: nothing set means the record validated clean."""

    __slots__ = ("drop_errors", "drop_ids", "errors")

    def __init__(
        self,
        drop_ids: list[str] | None = None,
        drop_errors: list[str] | None = None,
        errors: list[str] | None = None,
    ) -> None:
        self.drop_ids = drop_ids or []
        self.drop_errors = drop_errors or []
        self.errors = errors or []


def _validate_record(record: dict[str, Any]) -> _Outcome | None:
    """
    Validate one record's full setup in this (forked) process.

    Returns ``None`` when the gate doesn't apply (pin-conflict boards keep
    partial bundles, so no combined setup exists to validate).
    """
    from esphome.config import load_config
    from esphome.core import CORE, EsphomeError

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
    esphome_cfg = _load_esphome_config(record["esphome"], record["id"])
    if esphome_cfg.platform.value == "esp32" and esphome_cfg.variant is None:
        # sync_boards backfills the variant from the PIO board id when it
        # regenerates the catalog; without the same backfill an imported
        # ``board:``-only manifest fails as "This board is unknown".
        from esphome.components.esp32.boards import BOARDS

        from esphome_device_builder.models.boards import Esp32Variant

        meta = BOARDS.get(esphome_cfg.board)
        if meta is not None:
            esphome_cfg.variant = Esp32Variant(meta["variant"].lower())
    board = BoardCatalogEntry(
        id=record["id"],
        name=record["name"],
        description="",
        manufacturer="",
        esphome=esphome_cfg,
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

    from esphome_device_builder.helpers.device_yaml import generate_device_yaml

    with tempfile.TemporaryDirectory() as tmp:
        yaml_path = Path(tmp) / f"{record['id']}.yaml"
        yaml_text = generate_device_yaml(
            "repro", "Repro", board, ssid="ssid", psk="password", defaults=defaults
        )
        yaml_path.write_text(yaml_text, encoding="utf-8")
        CORE.config_path = yaml_path
        try:
            errors = [str(err) for err in load_config({}, skip_external_update=True).errors]
        except EsphomeError as err:
            errors = [str(err)]
    if not errors:
        return _Outcome()
    return _map_errors(errors, yaml_text, record)


def _map_errors(errors: list[str], yaml_text: str, record: dict[str, Any]) -> _Outcome:
    """Map each error's config path to the featured entry that produced it."""
    data = yaml.safe_load(yaml_text)
    entries = record.get("featured_components") or []
    local_ids = {entry["id"] for entry in entries}
    drop_ids: list[str] = []
    drop_errors: list[str] = []
    for error in errors:
        match = _ERROR_PATH_RE.search(error)
        local_id = _entry_for_path(match, data, entries, local_ids) if match else None
        if local_id is None:
            # An error we can't pin on one entry poisons the whole board.
            return _Outcome(errors=[error])
        if local_id not in drop_ids:
            drop_ids.append(local_id)
            drop_errors.append(error)
    return _Outcome(drop_ids=drop_ids, drop_errors=drop_errors)


def _entry_for_path(
    match: re.Match[str],
    data: dict[str, Any],
    entries: list[dict[str, Any]],
    local_ids: set[str],
) -> str | None:
    """Resolve one ``data['<domain>'][i]`` path to a featured local id."""
    domain, index = match.group(1), match.group(2)
    block = data.get(domain)
    item: Any = None
    if index is not None and isinstance(block, list):
        i = int(index)
        item = block[i] if i < len(block) else None
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


def _apply_drops(record: dict[str, Any], drop_ids: set[str]) -> None:
    """Remove *drop_ids* from the record's featured entries, bundles, and requires."""
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
