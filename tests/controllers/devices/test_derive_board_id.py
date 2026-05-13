"""End-to-end coverage for ``DevicesController._derive_board_id_from_yaml``.

The helper is invoked from ``_resolve_device_metadata`` whenever a
device lacks a sidecar ``board_id``. It parses the on-disk YAML for
``platform``/``board``/``variant``, asks the catalog for a matching
entry (preferring a PlatformIO-board match over a bare platform
fallback), and persists the result so the next scan skips the
YAML parse entirely.

Six branches to pin:

1. ``self._db.boards is None`` → empty string (the catalog hasn't
   loaded yet).
2. Missing YAML (``OSError``) → empty string (the file disappeared
   between the scanner and this read).
3. PlatformIO-board match → catalog id returned + persisted to the
   sidecar.
4. ``pio_board`` doesn't match but ``platform`` does → fallback hit,
   sidecar still gets backfilled.
5. No match at all → empty string, nothing written to the sidecar.
6. ``set_device_metadata`` raising → swallowed with a warning,
   the matched id still gets returned to the caller.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from esphome_device_builder.controllers.config import get_device_metadata

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
    ``esp32:`` fallback would pick. Pin both call ordering and
    the persist-to-sidecar step.
    """
    controller = make_controller(tmp_path, with_boards=True)
    boards = StubBoardLookups(controller)
    pio_lookup = boards.find_by_pio_board_returns("generic-esp32c3")
    platform_lookup = boards.find_by_platform_variant_returns(None)
    _write_yaml(tmp_path, "kitchen.yaml", platform="esp32", board="esp32-c3-devkitm-1")

    result = controller._derive_board_id_from_yaml(tmp_path, "kitchen.yaml")

    assert result == "generic-esp32c3"
    # PlatformIO match was tried.
    pio_lookup.assert_called_once_with("esp32-c3-devkitm-1", "")
    # Platform fallback wasn't reached.
    platform_lookup.assert_not_called()
    # Sidecar got backfilled so the next scan skips the YAML parse.
    meta = get_device_metadata(tmp_path, "kitchen.yaml")
    assert meta == {"board_id": "generic-esp32c3"}


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
    pio_lookup.assert_called_once_with("unknown-board", "")
    platform_lookup.assert_called_once_with("esp32", "")
    meta = get_device_metadata(tmp_path, "kitchen.yaml")
    assert meta == {"board_id": "generic-esp32"}


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


def test_derive_returns_empty_when_no_catalog_entry_matches(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Both lookups miss → empty string, sidecar untouched.

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
    # Sidecar is empty — nothing was persisted.
    meta = get_device_metadata(tmp_path, "kitchen.yaml")
    assert meta == {}


def test_derive_swallows_persist_failure_and_still_returns_id(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``set_device_metadata`` raising → warning logged, id still returned.

    Catalog match succeeded; we shouldn't drop the answer just
    because the sidecar write failed (e.g. read-only mount,
    permissions). Pin the warning so an operator gets the
    ``Could not persist derived board_id`` log line.

    Sync test even though the rest of the test file mixes sync and
    async — ``_derive_board_id_from_yaml`` itself is sync (production
    calls it from inside ``loop.run_in_executor`` via the scanner's
    metadata resolver, so blockbuster never sees the ``read_text``).
    Calling it directly from an async test trips blockbuster on the
    sync I/O even though production is fine.
    """
    controller = make_controller(tmp_path, with_boards=True)
    StubBoardLookups(controller).find_by_pio_board_returns("generic-esp32c3")
    _write_yaml(tmp_path, "kitchen.yaml", platform="esp32", board="esp32-c3-devkitm-1")

    def _boom(*args: object, **kwargs: object) -> None:
        msg = "filesystem read-only"
        raise OSError(msg)

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.metadata.set_device_metadata",
        _boom,
    )

    with caplog.at_level("WARNING"):
        result = controller._derive_board_id_from_yaml(tmp_path, "kitchen.yaml")

    assert result == "generic-esp32c3"
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("kitchen.yaml" in m for m in warnings), warnings
