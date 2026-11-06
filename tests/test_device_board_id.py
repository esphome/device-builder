"""A device's board id, derived from its merged config at load time."""

from __future__ import annotations

from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

from esphome_device_builder.controllers.boards import BoardCatalog
from esphome_device_builder.controllers.devices import DevicesController
from esphome_device_builder.helpers.device_yaml import load_device_from_storage
from esphome_device_builder.helpers.device_yaml._chip import resolve_board_fields
from esphome_device_builder.models import Device


@pytest.fixture(scope="module")
def catalog() -> BoardCatalog:
    """Load the real on-disk board catalog once for the module."""
    cat = BoardCatalog()
    cat.load()
    return cat


@pytest.mark.parametrize(
    ("config", "yaml_content", "platform", "subs", "storage_variant", "expected"),
    [
        pytest.param(
            {"esp32": {"variant": "ESP32S3"}}, "", "esp32", None, None, ("", "esp32s3"),
            id="variant_in_merged_config",
        ),
        pytest.param(
            {"esp32": {"board": "lolin_s3_mini"}}, "", "esp32", None, None,
            ("lolin_s3_mini", "esp32s3"),
            id="variant_from_board_table",
        ),
        pytest.param(
            {"esp32": {"framework": {"type": "esp-idf"}}}, "", "esp32", None, "ESP32C3",
            ("", "esp32c3"),
            id="variant_from_last_compile",
        ),
        pytest.param(
            {"esp32": {"framework": {"type": "esp-idf"}}}, "", "esp32", None, "ESP32", ("", ""),
            id="bare_family_from_storage_is_unknown",
        ),
        pytest.param(
            {"esp32": {"board": "lolin_s3_mini"}}, "", "esp32", None, "ESP32C3",
            ("lolin_s3_mini", "esp32c3"),
            id="last_compile_beats_board_table",
        ),
        pytest.param(
            {"esp32": {"board": "${chip_board}"}}, "", "esp32", {"chip_board": "esp32dev"}, None,
            ("esp32dev", "esp32"),
            id="board_substitution",
        ),
        pytest.param(
            None, "esp32:\n  variant: esp32s3\n", "esp32", None, None, ("", "esp32s3"),
            id="raw_text_without_merged_config",
        ),
        pytest.param(
            {"esp8266": {"board": "d1_mini"}}, "", "esp8266", None, "ESP8266", ("d1_mini", ""),
            id="non_esp32_ignores_compile_variant",
        ),
    ],
)  # fmt: skip
def test_resolve_board_fields(
    config: dict | None,
    yaml_content: str,
    platform: str,
    subs: dict[str, str] | None,
    storage_variant: str | None,
    expected: tuple[str, str],
) -> None:
    """The board and chip come from the merged config, raw text, then the board table."""
    assert resolve_board_fields(config, yaml_content, platform, subs, storage_variant) == expected


@pytest.mark.parametrize(
    ("main_platform_block", "package"),
    [
        pytest.param(
            "esp32:\n  framework:\n    type: esp-idf\n",
            "esp32:\n  variant: ESP32S3\n",
            id="variant_in_package",
        ),
        pytest.param("", "esp32:\n  variant: ESP32S3\n", id="whole_platform_block_in_package"),
    ],
)
def test_board_from_package_resolves_to_its_chip(
    tmp_path: Path, catalog: BoardCatalog, main_platform_block: str, package: str
) -> None:
    """An esp32 chip named only inside a package picks a board for that chip."""
    device = _load(tmp_path, catalog, main_platform_block, package)

    board = catalog.get_by_id(device.board_id)
    assert board is not None
    assert board.esphome.variant == "esp32s3"


@pytest.mark.parametrize(
    ("package", "board_id", "expected"),
    [
        pytest.param("esp32:\n  board: lolin_s3_mini\n", "", "lolin_s3_mini", id="in_package"),
        pytest.param("esp32:\n  variant: ESP32S3\n", "esp32-poe", "esp32-poe", id="user_pick_kept"),
    ],
)
def test_board_id_exact(
    tmp_path: Path, catalog: BoardCatalog, package: str, board_id: str, expected: str
) -> None:
    """A ``board:`` in a package resolves to that board; a user pick skips derivation."""
    assert _load(tmp_path, catalog, "", package, board_id=board_id).board_id == expected


def _load(
    tmp_path: Path,
    catalog: BoardCatalog,
    main_platform_block: str,
    package: str,
    board_id: str = "",
) -> Device:
    """Load a device whose ``packages:`` includes *package*, deriving against *catalog*."""
    (tmp_path / "hardware.yaml").write_text(package, encoding="utf-8")
    path = tmp_path / "device.yaml"
    path.write_text(
        "esphome:\n  name: device\n"
        + main_platform_block
        + "packages:\n  hardware: !include hardware.yaml\n",
        encoding="utf-8",
    )
    controller = SimpleNamespace(_db=SimpleNamespace(boards=catalog))
    derive = partial(DevicesController._derive_board_id, controller)
    return load_device_from_storage(path, board_id, derive_board_id=derive)
