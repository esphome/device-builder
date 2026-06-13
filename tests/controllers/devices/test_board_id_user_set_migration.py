"""One-shot back-fill of ``board_id_user_set`` for pre-flag sidecars.

Runs against the real catalog: a curated pick now outranked by a
generic on its own PlatformIO board (apollo-esk-1 vs the generic
esp32-c6-devkitm-1) must be preserved, while a devices.esphome.io
import (the stale auto-derived shape) is left to re-derive.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from esphome_device_builder.controllers.boards import BoardCatalog
from esphome_device_builder.controllers.config import (
    _load_metadata,
    get_device_metadata,
    set_device_metadata,
)
from esphome_device_builder.controllers.devices import DevicesController
from esphome_device_builder.controllers.devices._metadata_store import DeviceMetadataStore
from esphome_device_builder.controllers.devices._shared_sidecar import SharedSidecarClient
from esphome_device_builder.controllers.devices.metadata import (
    _USER_SET_MIGRATED_KEY,
    _migrate_board_id_user_set_sync,
)

_APOLLO = "apollo-esk-1"
_AVATTO = "avatto_s06_ir_remote_no_temp_no_humidity_new_version"


def _controller(tmp_path: Path, catalog: BoardCatalog | None) -> DevicesController:
    """Build a bare controller wired to a real catalog for migrate + resolve."""
    controller = DevicesController.__new__(DevicesController)
    controller._shutdown_callbacks = []
    controller._metadata_store = DeviceMetadataStore(
        config_dir=tmp_path,
        data_dir=tmp_path,
        shutdown_register=controller._shutdown_callbacks.append,
    )
    controller._shared_sidecar = SharedSidecarClient(tmp_path)
    controller._db = SimpleNamespace(boards=catalog, settings=SimpleNamespace(config_dir=tmp_path))
    return controller


@pytest.fixture(scope="module")
def real_catalog() -> BoardCatalog:
    """Load the real on-disk catalog once for the module."""
    cat = BoardCatalog()
    cat.load()
    return cat


def test_curated_pick_displaced_by_generic_is_flagged(
    tmp_path: Path, real_catalog: BoardCatalog
) -> None:
    """A pre-flag apollo-esk-1 pick is stamped user-set so it isn't re-derived."""
    set_device_metadata(tmp_path, "apollo.yaml", board_id=_APOLLO)

    stamped = _migrate_board_id_user_set_sync(tmp_path, real_catalog)

    assert stamped == 1
    assert get_device_metadata(tmp_path, "apollo.yaml")["board_id_user_set"] is True


def test_devices_esphome_io_import_is_left_unflagged(
    tmp_path: Path, real_catalog: BoardCatalog
) -> None:
    """An imported vendor board is the stale shape; it stays unflagged to re-derive."""
    set_device_metadata(tmp_path, "avatto.yaml", board_id=_AVATTO)

    _migrate_board_id_user_set_sync(tmp_path, real_catalog)

    assert "board_id_user_set" not in get_device_metadata(tmp_path, "avatto.yaml")


def test_generic_board_id_is_left_unflagged(tmp_path: Path, real_catalog: BoardCatalog) -> None:
    """A board_id that already names the generic has no conflict; not flagged."""
    set_device_metadata(tmp_path, "generic.yaml", board_id="esp32-c6-devkitm-1")

    _migrate_board_id_user_set_sync(tmp_path, real_catalog)

    assert "board_id_user_set" not in get_device_metadata(tmp_path, "generic.yaml")


def test_already_flagged_pick_is_untouched(tmp_path: Path, real_catalog: BoardCatalog) -> None:
    """An entry already flagged user-set is not re-counted or altered."""
    set_device_metadata(tmp_path, "picked.yaml", board_id=_APOLLO, board_id_user_set=True)

    stamped = _migrate_board_id_user_set_sync(tmp_path, real_catalog)

    assert stamped == 0
    assert get_device_metadata(tmp_path, "picked.yaml")["board_id_user_set"] is True


def test_import_with_generic_winner_is_left_unflagged(
    tmp_path: Path, real_catalog: BoardCatalog
) -> None:
    """An import is left unflagged even when its pio now resolves to a generic.

    Aubess's board resolves to a generic BK7231N, so without the
    import guard it would be stamped like a curated pick; the import
    check is what keeps it healing instead.
    """
    set_device_metadata(
        tmp_path, "bktest.yaml", board_id="aubess_wifi_smart_switch_with_power_monitoring"
    )

    assert _migrate_board_id_user_set_sync(tmp_path, real_catalog) == 0
    assert "board_id_user_set" not in get_device_metadata(tmp_path, "bktest.yaml")


def test_curated_pick_displaced_by_non_generic_is_left_unflagged(
    tmp_path: Path, real_catalog: BoardCatalog
) -> None:
    """A curated pick outranked by another non-generic board is not preserved.

    The migration only rescues picks displaced by a generic; LOLIN
    S2 Mini redrawing as the canonical WEMOS LOLIN S2 Mini (both
    non-generic) re-derives instead of pinning.
    """
    set_device_metadata(tmp_path, "lolin.yaml", board_id="esp32-s2-mini")

    assert _migrate_board_id_user_set_sync(tmp_path, real_catalog) == 0
    assert "board_id_user_set" not in get_device_metadata(tmp_path, "lolin.yaml")


def test_pick_that_still_wins_its_own_board_is_left_unflagged(
    tmp_path: Path, real_catalog: BoardCatalog
) -> None:
    """A board still canonical for its own pio has no conflict, so no flag."""
    set_device_metadata(tmp_path, "olimex.yaml", board_id="esp32-poe-iso")

    assert _migrate_board_id_user_set_sync(tmp_path, real_catalog) == 0
    assert "board_id_user_set" not in get_device_metadata(tmp_path, "olimex.yaml")


def test_migration_is_one_shot(tmp_path: Path, real_catalog: BoardCatalog) -> None:
    """The sidecar marker stops a second pass from stamping later entries."""
    set_device_metadata(tmp_path, "apollo.yaml", board_id=_APOLLO)
    assert _migrate_board_id_user_set_sync(tmp_path, real_catalog) == 1

    # A curated pick added after the first run is NOT back-filled.
    set_device_metadata(tmp_path, "apollo2.yaml", board_id=_APOLLO)
    assert _migrate_board_id_user_set_sync(tmp_path, real_catalog) == 0
    assert "board_id_user_set" not in get_device_metadata(tmp_path, "apollo2.yaml")


async def test_migrate_then_resolve_matches_fleet_outcome(
    tmp_path: Path, real_catalog: BoardCatalog
) -> None:
    """End-to-end through the real resolver: cb3s heals, the Apollo pick is preserved.

    Mirrors the two headline cases from the dry-run: a stale import
    (cb3s-test pinned to AVATTO) re-derives to CB3S, while a curated
    Apollo pick with no YAML on disk (aps.yaml) survives via the
    migration stamp.
    """

    def _seed() -> None:
        set_device_metadata(tmp_path, "aps.yaml", board_id=_APOLLO)
        set_device_metadata(tmp_path, "cb3s-test.yaml", board_id=_AVATTO)
        (tmp_path / "cb3s-test.yaml").write_text(
            "esphome:\n  name: cb3s-test\n\nbk72xx:\n  board: cb3s\n", encoding="utf-8"
        )

    await asyncio.to_thread(_seed)
    controller = _controller(tmp_path, real_catalog)
    await controller.migrate_board_id_user_set()

    def _resolved_board_ids() -> tuple[str, str]:
        return (
            controller._resolve_device_metadata(tmp_path, "aps.yaml").board_id,
            controller._resolve_device_metadata(tmp_path, "cb3s-test.yaml").board_id,
        )

    apollo_board, cb3s_board = await asyncio.to_thread(_resolved_board_ids)

    assert apollo_board == _APOLLO
    assert cb3s_board == "cb3s"


async def test_curated_esp8266_pick_survives_exact_id_derive(
    tmp_path: Path, real_catalog: BoardCatalog
) -> None:
    """Migration stamps a curated esp8266 pick before exact-id derive re-resolves it."""

    def _seed() -> None:
        set_device_metadata(tmp_path, "sonoff.yaml", board_id="sonoff-basic")
        (tmp_path / "sonoff.yaml").write_text(
            "esphome:\n  name: sonoff\n\nesp8266:\n  board: esp01_1m\n", encoding="utf-8"
        )

    await asyncio.to_thread(_seed)
    controller = _controller(tmp_path, real_catalog)
    await controller.migrate_board_id_user_set()

    resolved = await asyncio.to_thread(
        lambda: controller._resolve_device_metadata(tmp_path, "sonoff.yaml").board_id
    )
    assert resolved == "sonoff-basic"


async def test_migrate_is_noop_when_catalog_unloaded(tmp_path: Path) -> None:
    """With no catalog yet, the migration is a no-op and writes no marker.

    The marker is withheld so the back-fill still runs on a later boot
    once the catalog has loaded.
    """
    await asyncio.to_thread(set_device_metadata, tmp_path, "apollo.yaml", board_id=_APOLLO)

    controller = _controller(tmp_path, None)
    await controller.migrate_board_id_user_set()

    def _entry_and_root() -> tuple[dict[str, Any], dict[str, Any]]:
        return get_device_metadata(tmp_path, "apollo.yaml"), _load_metadata(tmp_path)

    entry, root = await asyncio.to_thread(_entry_and_root)
    assert "board_id_user_set" not in entry
    assert _USER_SET_MIGRATED_KEY not in root


def test_migration_skips_non_device_keys(tmp_path: Path, real_catalog: BoardCatalog) -> None:
    """Top-level meta keys (``_labels`` etc.) are skipped, not parsed as devices."""
    set_device_metadata(tmp_path, "apollo.yaml", board_id=_APOLLO)
    path = tmp_path / ".device-builder.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["_labels"] = [{"id": "x", "name": "Office"}]
    path.write_text(json.dumps(data), encoding="utf-8")

    assert _migrate_board_id_user_set_sync(tmp_path, real_catalog) == 1
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["_labels"] == [{"id": "x", "name": "Office"}]
