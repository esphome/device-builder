"""Unit tests for ``_bus_constraints_from_source`` in ``script/sync_components.py``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import orjson

from script.sync_components import (  # type: ignore[import-not-found]
    _CURATED_BUS_CONSTRAINTS,
    _OUTPUT_BODIES_DIR,
    _apply_curated_bus_constraints,
    _bus_constraints_from_source,
)


def _write(tmp_path: Path, source: str, name: str = "module.py") -> str:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return str(path)


def test_i2c_frequency_normalises_to_hz(tmp_path: Path) -> None:
    """A unit-suffixed frequency string lands as plain Hz."""
    src = _write(
        tmp_path,
        "FINAL_VALIDATE_SCHEMA = i2c.final_validate_device_schema(\n"
        '    "ags10", max_frequency="15khz"\n'
        ")\n",
    )
    assert _bus_constraints_from_source(src) == {"i2c": {"max_frequency": 15000.0}}


def test_uart_exact_match_values_and_required_pins(tmp_path: Path) -> None:
    """Literal uart kwargs survive; ``False`` requires and ``uart_bus`` drop."""
    src = _write(
        tmp_path,
        "FINAL_VALIDATE_SCHEMA = uart.final_validate_device_schema(\n"
        '    "dfplayer", uart_bus="alt_uart", baud_rate=9600, require_tx=True, require_rx=False\n'
        ")\n",
    )
    assert _bus_constraints_from_source(src) == {"uart": {"baud_rate": 9600, "require_tx": True}}


def test_parity_none_drops_but_parity_string_none_stays(tmp_path: Path) -> None:
    """Python ``None`` means unconstrained; the string "NONE" is a real constraint."""
    src = _write(
        tmp_path,
        "FINAL_VALIDATE_SCHEMA = uart.final_validate_device_schema(\n"
        '    "x", parity=None, stop_bits=1\n'
        ")\n",
    )
    assert _bus_constraints_from_source(src) == {"uart": {"stop_bits": 1}}

    src2 = _write(
        tmp_path,
        'FINAL_VALIDATE_SCHEMA = uart.final_validate_device_schema("x", parity="NONE")\n',
        name="module2.py",
    )
    assert _bus_constraints_from_source(src2) == {"uart": {"parity": "NONE"}}


def test_timeout_normalises_to_milliseconds(tmp_path: Path) -> None:
    """A unit-suffixed timeout string lands as plain milliseconds."""
    src = _write(
        tmp_path,
        'FINAL_VALIDATE_SCHEMA = i2c.final_validate_device_schema("x", max_timeout="10ms")\n',
    )
    assert _bus_constraints_from_source(src) == {"i2c": {"max_timeout": 10}}


def test_unstructured_final_validate_yields_nothing(tmp_path: Path) -> None:
    """A hand-written validator function is not a bus constraint."""
    src = _write(tmp_path, "FINAL_VALIDATE_SCHEMA = _final_validate\n")
    assert _bus_constraints_from_source(src) == {}


def test_non_bus_helper_is_ignored(tmp_path: Path) -> None:
    """Only the i2c/spi/uart helpers carry machine-readable constraints."""
    src = _write(
        tmp_path,
        'FINAL_VALIDATE_SCHEMA = modbus.final_validate_device_schema("x", baud_rate=9600)\n',
    )
    assert _bus_constraints_from_source(src) == {}


def test_cv_all_wrapped_final_validate_is_unwrapped(tmp_path: Path) -> None:
    """A ``cv.All(uart.final_validate_device_schema(...), ...)`` wrapper is still read (cn105)."""
    src = _write(
        tmp_path,
        "FINAL_VALIDATE_SCHEMA = cv.All(\n"
        '    uart.final_validate_device_schema("cn105", parity="EVEN", require_rx=True),\n'
        "    _extra_validate,\n"
        ")\n",
    )
    assert _bus_constraints_from_source(src) == {"uart": {"parity": "EVEN", "require_rx": True}}


def test_curated_cn105_is_a_baud_choice_list() -> None:
    """CN105's rate is heat-pump-dependent, so it's curated as a 2400/9600 choice."""
    assert _CURATED_BUS_CONSTRAINTS["climate.mitsubishi_cn105"]["uart"]["baud_rate"] == [2400, 9600]


def test_curated_fixed_baud_rows_are_scalars() -> None:
    """The stopgap fixed-baud rows carry the rate each device's hardware needs."""
    expected = {
        "sensor.bl0940": 4800,
        "sensor.pzem004t": 9600,
        "rdm6300": 9600,
        "fingerprint_grow": 57600,
        "rf_bridge": 19200,
        "light.shelly_dimmer": 115200,
        "sim800l": 9600,
    }
    for cid, baud in expected.items():
        assert _CURATED_BUS_CONSTRAINTS[cid]["uart"]["baud_rate"] == baud


def test_apply_curated_merges_onto_captured_constraints() -> None:
    """Curated baud merges with sim800l's captured require_* instead of replacing them."""
    captured: dict[str, dict[str, Any]] = {"uart": {"require_tx": True, "require_rx": True}}
    _apply_curated_bus_constraints("sim800l", captured)
    assert captured == {"uart": {"require_tx": True, "require_rx": True, "baud_rate": 9600}}


def test_apply_curated_does_not_overwrite_a_captured_key() -> None:
    """Once upstream validates the rate, the captured value wins over the curated stopgap."""
    # bl0940's curated stopgap is 4800; a captured (upstream) value must survive.
    captured: dict[str, dict[str, Any]] = {"uart": {"baud_rate": 19200}}
    _apply_curated_bus_constraints("sensor.bl0940", captured)
    assert captured["uart"]["baud_rate"] == 19200


def test_apply_curated_is_noop_for_unlisted_component() -> None:
    """A component with no curated entry is left untouched."""
    constraints: dict[str, dict[str, Any]] = {}
    _apply_curated_bus_constraints("sensor.dht", constraints)
    assert constraints == {}


def test_shipped_catalog_carries_curated_baud() -> None:
    """The generated bodies merge the curated baud (CN105 list, sim800l onto its require_*)."""
    cn105 = orjson.loads((_OUTPUT_BODIES_DIR / "climate.mitsubishi_cn105.json").read_bytes())
    assert cn105["bus_constraints"]["uart"]["baud_rate"] == [2400, 9600]
    sim = orjson.loads((_OUTPUT_BODIES_DIR / "sim800l.json").read_bytes())
    assert sim["bus_constraints"]["uart"]["baud_rate"] == 9600
    assert sim["bus_constraints"]["uart"]["require_tx"] is True


def test_shipped_catalog_captures_cn105_cv_all_constraints() -> None:
    """CN105's cv.All-wrapped FINAL_VALIDATE constraints land beside the curated baud."""
    cn105 = orjson.loads((_OUTPUT_BODIES_DIR / "climate.mitsubishi_cn105.json").read_bytes())
    uart = cn105["bus_constraints"]["uart"]
    assert uart["parity"] == "EVEN"
    assert uart["require_rx"] is True
    assert uart["require_tx"] is True
