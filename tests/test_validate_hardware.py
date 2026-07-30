"""Pin the best-effort hardware convention warnings."""

from __future__ import annotations

import pytest

import script.validate_definitions as vd


@pytest.fixture(autouse=True)
def _no_esphome_tables(monkeypatch):
    """Pin the table cache to esphome-absent; table tests monkeypatch their own."""
    for platform in ("esp32", "esp8266", "rp2040"):
        monkeypatch.setitem(vd._ESPHOME_BOARDS_CACHE, platform, None)


def _board(platform: str, board: str, **kwargs) -> dict:
    esphome = {"platform": platform, "board": board}
    if variant := kwargs.pop("variant", None):
        esphome["variant"] = variant
    if mcu := kwargs.pop("mcu", None):
        esphome["mcu"] = mcu
    return {"esphome": esphome, "hardware": kwargs}


def test_ram_matching_chip_is_quiet():
    data = _board("esp32", "esp32-c3-devkitm-1", variant="esp32c3", ram_size=327680)
    assert vd.collect_hardware_warnings("b", data) == []


def test_ram_datasheet_figure_warns():
    data = _board("esp32", "esp32-c3-devkitm-1", variant="esp32c3", ram_size=393216)
    warnings = vd.collect_hardware_warnings("b", data)
    assert len(warnings) == 1
    assert "327680" in warnings[0]


def test_ram_psram_inclusive_is_quiet():
    data = _board("esp32", "m5stack-core2", variant="esp32", ram_size=4521984)
    assert vd.collect_hardware_warnings("b", data) == []


def test_ram_per_chip_values():
    for platform, board, chip_kw, ram in [
        ("esp8266", "d1_mini", {}, 81920),
        ("rp2", "rpipico", {"mcu": "rp2040"}, 262144),
        ("rp2", "rpipico2", {"mcu": "rp2350"}, 524288),
    ]:
        data = _board(platform, board, ram_size=ram, **chip_kw)
        assert vd.collect_hardware_warnings("b", data) == []
        data = _board(platform, board, ram_size=ram + 1, **chip_kw)
        assert len(vd.collect_hardware_warnings("b", data)) == 1


def test_wrong_declared_esp32_variant_warns(monkeypatch):
    monkeypatch.setitem(vd._ESPHOME_BOARDS_CACHE, "esp32", {"someboard": {"variant": "ESP32S3"}})
    data = _board("esp32", "someboard", variant="esp32c3")
    warnings = vd.collect_hardware_warnings("b", data)
    assert len(warnings) == 1
    assert "esp32s3" in warnings[0]


def test_esp8266_flash_mismatch_warns(monkeypatch):
    monkeypatch.setitem(vd._ESPHOME_BOARDS_CACHE, "esp8266", {"d1_mini": {"flash_size": 4194304}})
    data = _board("esp8266", "d1_mini", flash_size="1MB")
    warnings = vd.collect_hardware_warnings("b", data)
    assert len(warnings) == 1
    assert "4194304" in warnings[0]

    data = _board("esp8266", "d1_mini", flash_size="4MB")
    assert vd.collect_hardware_warnings("b", data) == []

    data = _board("esp8266", "d1_mini", flash_size="not-a-size")
    assert vd.collect_hardware_warnings("b", data) == []


def test_no_esphome_tables_skips_table_checks():
    data = _board("esp8266", "d1_mini", flash_size="1MB", ram_size=81920)
    assert vd.collect_hardware_warnings("b", data) == []
    # The stdlib-only ram check still runs off the declared variant.
    data = _board("esp32", "someboard", variant="esp32c3", ram_size=393216)
    assert len(vd.collect_hardware_warnings("b", data)) == 1


def test_unknown_chip_and_missing_hardware_are_quiet():
    assert vd.collect_hardware_warnings("b", {"esphome": {"platform": "bk72xx"}}) == []
    data = _board("bk72xx", "cb2s", ram_size=1)
    assert vd.collect_hardware_warnings("b", data) == []


def test_malformed_manifest_values_stay_best_effort():
    data = _board("rp2040", "rpipico", ram_size=262145)
    data["esphome"]["mcu"] = {"not": "a string"}
    assert vd.collect_hardware_warnings("b", data) == []
    data = _board("esp32", "someboard", ram_size=262145)
    data["esphome"]["variant"] = ["esp32c3"]
    assert vd.collect_hardware_warnings("b", data) == []


def test_flash_str_to_bytes():
    assert vd._flash_str_to_bytes("4MB") == 4194304
    assert vd._flash_str_to_bytes("0.5MB") == 524288
    assert vd._flash_str_to_bytes("512KB") is None
