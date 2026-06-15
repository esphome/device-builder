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
import tempfile
from pathlib import Path

import pytest
from esphome.config import load_config
from esphome.core import CORE, EsphomeError

from esphome_device_builder.definitions import (
    load_board_body_from_disk,
    load_board_index,
)
from esphome_device_builder.helpers.device_yaml import generate_device_yaml

pytestmark = [
    pytest.mark.timeout(600),
    pytest.mark.skipif(
        "fork" not in mp.get_all_start_methods(),
        reason="needs os.fork for per-board ESPHome state isolation",
    ),
]


def _validate_board(board_id: str) -> tuple[str, list[str]]:
    """Validate one board's generated YAML in a fresh process; return its errors."""
    board = load_board_body_from_disk(board_id)
    assert board is not None, board_id
    with tempfile.TemporaryDirectory() as tmp:
        # Inline creds keep the YAML ``!secret``-free so it validates standalone.
        yaml_path = Path(tmp) / f"{board_id}.yaml"
        yaml_path.write_text(
            generate_device_yaml("repro", "Repro", board, ssid="ssid", psk="password"),
            encoding="utf-8",
        )
        CORE.config_path = yaml_path
        try:
            return board_id, [str(err) for err in load_config({}, skip_external_update=True).errors]
        except EsphomeError as err:
            return board_id, [str(err)]


def test_every_board_creates_a_valid_config() -> None:
    board_ids = [entry.id for entry in load_board_index()]
    ctx = mp.get_context("fork")
    with ctx.Pool(processes=min(8, os.cpu_count() or 4), maxtasksperchild=1) as pool:
        results = pool.map(_validate_board, board_ids, chunksize=1)

    failures = {board_id: errors for board_id, errors in results if errors}
    assert not failures, "boards fail ESPHome validation:\n" + "\n".join(
        f"{board_id}: {'; '.join(errs)}" for board_id, errs in sorted(failures.items())
    )
