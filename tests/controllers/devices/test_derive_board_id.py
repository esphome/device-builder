"""End-to-end coverage for ``DevicesController._derive_board_id_from_yaml``.

The helper is invoked from ``_resolve_device_metadata`` whenever a
device has no user-set sidecar ``board_id``. It parses the YAML for
``platform`` / ``board`` / ``variant`` and asks the catalog for a
matching entry. The result is never persisted; it is recomputed each
resolve so a catalog update self-heals.

Branches to pin:

1. ``self._db.boards is None`` → empty string (catalog not loaded).
2. Missing YAML (``OSError``) → empty string.
3. PlatformIO-board match → catalog id returned.
4. ``pio_board`` misses, ``platform`` matches → fallback hit.
5. No match at all → empty string.
6. A catalog hit doesn't write the sidecar.
"""

from __future__ import annotations

from pathlib import Path

from .conftest import MakeControllerFactory, StubBoardLookups


def _write_yaml(tmp_path: Path, filename: str, *, platform: str, board: str = "") -> Path:
    """Drop a YAML the parser can read on disk and return its path."""
    body = f"esphome:\n  name: kitchen\n\n{platform}:\n"
    if board:
        body += f"  board: {board}\n"
    path = tmp_path / filename
    path.write_text(body, encoding="utf-8")
    return path


def test_derive_returns_empty_when_boards_catalog_unloaded(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """``boards is None`` → empty string, no YAML read attempted.

    Pin the early-return guard. The catalog can be ``None`` during
    a brief startup window before ``BoardCatalog.load()`` finishes;
    the scanner shouldn't crash if it sees a device in that
    window.
    """
    controller = make_controller(tmp_path, with_boards=False)
    controller._db.boards = None

    result = controller._derive_board_id_from_yaml(tmp_path, "kitchen.yaml")

    assert result == ""


def test_derive_returns_empty_on_missing_yaml(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A YAML that disappeared between scan and read → empty string.

    The scanner can list a YAML that gets atomic-saved (briefly
    unlinked) before the metadata-resolver reads it. The
    ``OSError`` branch is the silent-fallback that keeps the
    metadata pass alive instead of crashing the whole scan.
    """
    controller = make_controller(tmp_path, with_boards=True)

    result = controller._derive_board_id_from_yaml(tmp_path, "ghost.yaml")

    assert result == ""


def test_derive_uses_pio_board_match_first(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """When the YAML carries a ``board:`` field, that wins over platform fallback.

    ``find_by_pio_board`` is the higher-fidelity lookup — a YAML
    with ``board: esp32-c3-devkitm-1`` should land on the
    "Generic ESP32-C3" catalog entry, not on whatever the bare
    ``esp32:`` fallback would pick. Pin the call ordering.
    """
    controller = make_controller(tmp_path, with_boards=True)
    boards = StubBoardLookups(controller)
    pio_lookup = boards.find_by_pio_board_returns("generic-esp32c3")
    platform_lookup = boards.find_by_platform_variant_returns(None)
    _write_yaml(tmp_path, "kitchen.yaml", platform="esp32", board="esp32-c3-devkitm-1")

    result = controller._derive_board_id_from_yaml(tmp_path, "kitchen.yaml")

    assert result == "generic-esp32c3"
    # PlatformIO match was tried.
    pio_lookup.assert_called_once_with("esp32-c3-devkitm-1", "", "esp32")
    # Platform fallback wasn't reached.
    platform_lookup.assert_not_called()


def test_derive_falls_back_to_platform_when_pio_board_misses(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """``board:`` present but unknown → ``find_by_platform_variant`` runs.

    Pin the actual fallback chain: a YAML with a ``board:`` field
    whose value the catalog doesn't recognise should still try the
    coarser platform lookup before giving up. Without the explicit
    ``board:``, ``_derive_board_id_from_yaml`` skips the
    ``find_by_pio_board`` call entirely (see the no-board-specified
    test below for that path), so a test without a ``board:`` field
    wouldn't actually pin the "miss-then-fallback" branch this is
    about.
    """
    controller = make_controller(tmp_path, with_boards=True)
    boards = StubBoardLookups(controller)
    pio_lookup = boards.find_by_pio_board_returns(None)
    platform_lookup = boards.find_by_platform_variant_returns("generic-esp32")
    _write_yaml(tmp_path, "kitchen.yaml", platform="esp32", board="unknown-board")

    result = controller._derive_board_id_from_yaml(tmp_path, "kitchen.yaml")

    assert result == "generic-esp32"
    # Both lookups ran in order: PIO first, then platform fallback.
    pio_lookup.assert_called_once_with("unknown-board", "", "esp32")
    platform_lookup.assert_called_once_with("esp32", "")


def test_derive_skips_pio_board_when_yaml_omits_board_field(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """No ``board:`` in the YAML → ``find_by_pio_board`` is never called.

    Covers the YAML shape where the user only specified ``esp32:``
    without a ``board:`` field. The implementation skips the PIO
    lookup entirely (gated on truthy ``pio_board``) and goes
    straight to the platform fallback.
    """
    controller = make_controller(tmp_path, with_boards=True)
    boards = StubBoardLookups(controller)
    pio_lookup = boards.find_by_pio_board_returns(None)
    platform_lookup = boards.find_by_platform_variant_returns("generic-esp32")
    _write_yaml(tmp_path, "kitchen.yaml", platform="esp32")

    result = controller._derive_board_id_from_yaml(tmp_path, "kitchen.yaml")

    assert result == "generic-esp32"
    # PIO lookup was skipped — pio_board was empty.
    pio_lookup.assert_not_called()
    platform_lookup.assert_called_once_with("esp32", "")


def test_derive_does_not_persist_to_sidecar(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A catalog match returns the id without writing it to the sidecar.

    A derived id depends on the shipped catalog, so it must not be
    cached; a stale write would survive a catalog update that
    changes the match.
    """
    controller = make_controller(tmp_path, with_boards=True)
    StubBoardLookups(controller).find_by_pio_board_returns("generic-esp32c3")
    _write_yaml(tmp_path, "kitchen.yaml", platform="esp32", board="esp32-c3-devkitm-1")

    result = controller._derive_board_id_from_yaml(tmp_path, "kitchen.yaml")

    assert result == "generic-esp32c3"
    assert controller._shared_sidecar.get_sync("kitchen.yaml") == {}


def test_derive_returns_empty_when_no_catalog_entry_matches(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Both lookups miss → empty string.

    A YAML with a totally unknown platform shouldn't poison the
    sidecar with junk — the next scan re-tries (in case the
    catalog was reloaded with new entries in the meantime).
    """
    controller = make_controller(tmp_path, with_boards=True)
    boards = StubBoardLookups(controller)
    boards.find_by_pio_board_returns(None)
    boards.find_by_platform_variant_returns(None)
    _write_yaml(tmp_path, "kitchen.yaml", platform="esp32", board="nonexistent-board")

    result = controller._derive_board_id_from_yaml(tmp_path, "kitchen.yaml")

    assert result == ""
