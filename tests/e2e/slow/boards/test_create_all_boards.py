"""
Every catalog board generates a config ESPHome accepts.

``generate_device_yaml`` feeds ``devices/create``; a board whose generated
YAML ESPHome rejects can't be created (#1486, where esp32 boards missing a
``variant`` produced an ``esp32:`` block with neither ``board`` nor
``variant``). This validates every board with the real
``esphome.config.load_config``.

Each board runs in its own forked worker (``maxtasksperchild=1``): ESPHome
accumulates module-global state across validations that ``CORE.reset()``
doesn't fully clear, which spuriously rejects later LibreTiny boards, so a
fresh process per board is the only reliable isolation. Fork-only, so it's
skipped on Windows; slow, so it's excluded from the default e2e run.
"""

from __future__ import annotations

import multiprocessing as mp
import os
from typing import TYPE_CHECKING, Any

import pytest

from esphome_device_builder.controllers.components import _load_body_from_disk
from esphome_device_builder.definitions import (
    load_board_body_from_disk,
    load_board_index,
)
from script._full_setup_gate import run_esphome_validation
from tests.conftest import catalog_releases_ahead

if TYPE_CHECKING:
    from esphome_device_builder.models import BoardCatalogEntry, ComponentCatalogEntry

pytestmark = [
    pytest.mark.timeout(600),
    pytest.mark.skipif(
        "fork" not in mp.get_all_start_methods(),
        reason="needs os.fork for per-board ESPHome state isolation",
    ),
]


def _defaults_from_disk(
    board: BoardCatalogEntry,
) -> list[tuple[ComponentCatalogEntry, dict[str, Any]]]:
    """
    Disk-only stand-in for ``ComponentCatalog.resolve_default_components``.

    Featured preset values plus inline overrides, no lock/suggestion
    handling — enough for the generated YAML to match a real create.
    """
    featured = {fc.id: fc for fc in board.featured_components}
    out: list[tuple[ComponentCatalogEntry, dict[str, Any]]] = []
    for entry in board.default_components:
        fc = featured.get(entry.id)
        body = _load_body_from_disk(fc.component_id if fc is not None else entry.id)
        assert body is not None, f"{board.id}: default component {entry.id!r} has no catalog body"
        fields: dict[str, Any] = {}
        if fc is not None:
            fields = {
                key: preset.value for key, preset in fc.fields.items() if preset.value is not None
            }
        fields.update(entry.fields)
        out.append((body, fields))
    return out


def _full_setup_from_disk(
    board: BoardCatalogEntry,
) -> list[tuple[ComponentCatalogEntry, dict[str, Any]]] | None:
    """
    Resolve the covering full-setup bundle the recommended-add flow installs.

    ``None`` when no bundle covers every featured component (pin-conflict
    boards keep partial bundles; the combined config wouldn't validate).
    """
    featured = {fc.id: fc for fc in board.featured_components}
    bundle = next(
        (b for b in board.featured_bundles if set(featured) <= set(b.component_ids)),
        None,
    )
    if bundle is None:
        return None
    out: list[tuple[ComponentCatalogEntry, dict[str, Any]]] = []
    for member in bundle.component_ids:
        fc = featured.get(member)
        assert fc is not None, f"{board.id}: bundle member {member!r} not featured"
        body = _load_body_from_disk(fc.component_id)
        assert body is not None, f"{board.id}: bundle member {member!r} has no catalog body"
        out.append((body, {k: p.value for k, p in fc.fields.items() if p.value is not None}))
    return out


def _generated_yaml_errors(
    board_id: str,
    board: BoardCatalogEntry,
    defaults: list[tuple[ComponentCatalogEntry, dict[str, Any]]],
) -> list[str]:
    """Run one generated YAML through the real ESPHome validation."""
    _, errors = run_esphome_validation(board_id, board, defaults)
    return [str(err) for err in errors]


def _validate_board(board_id: str) -> tuple[str, list[str]]:
    """Validate one board's generated YAML in a fresh process; return its errors."""
    board = load_board_body_from_disk(board_id)
    assert board is not None, board_id
    return board_id, _generated_yaml_errors(board_id, board, _defaults_from_disk(board))


def _validate_full_setup(board_id: str) -> tuple[str, list[str]]:
    """Validate a full_config board's covering-bundle YAML; empty when not applicable."""
    board = load_board_body_from_disk(board_id)
    assert board is not None, board_id
    if not board.full_config:
        return board_id, []
    defaults = _full_setup_from_disk(board)
    if defaults is None:
        return board_id, []
    return board_id, _generated_yaml_errors(board_id, board, defaults)


def test_every_board_creates_a_valid_config() -> None:
    board_ids = [entry.id for entry in load_board_index()]
    ctx = mp.get_context("fork")
    with ctx.Pool(processes=min(8, os.cpu_count() or 4), maxtasksperchild=1) as pool:
        results = pool.map(_validate_board, board_ids, chunksize=1)

    failures = {board_id: errors for board_id, errors in results if errors}
    # A board catalog generated from a newer esphome carries board ids the
    # installed release doesn't know yet; only that error is skew, every
    # other validation failure stays fatal. Strictness lives on the matrix
    # leg whose esphome matches the catalog stamp.
    if catalog_releases_ahead("boards.index.json") > 0:
        failures = {
            board_id: errors
            for board_id, errors in failures.items()
            if not all("This board is unknown" in err for err in errors)
        }
    assert not failures, "boards fail ESPHome validation:\n" + "\n".join(
        f"{board_id}: {'; '.join(errs)}" for board_id, errs in sorted(failures.items())
    )


def test_full_config_boards_validate_full_setup() -> None:
    board_ids = [entry.id for entry in load_board_index()]
    ctx = mp.get_context("fork")
    with ctx.Pool(processes=min(8, os.cpu_count() or 4), maxtasksperchild=1) as pool:
        results = pool.map(_validate_full_setup, board_ids, chunksize=1)

    failures = {board_id: errors for board_id, errors in results if errors}
    if catalog_releases_ahead("boards.index.json") > 0:
        failures = {
            board_id: errors
            for board_id, errors in failures.items()
            if not all("This board is unknown" in err for err in errors)
        }
    assert not failures, "full-setup bundles fail ESPHome validation:\n" + "\n".join(
        f"{board_id}: {'; '.join(errs)}" for board_id, errs in sorted(failures.items())
    )
