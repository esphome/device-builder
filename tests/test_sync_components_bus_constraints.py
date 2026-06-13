"""Unit tests for ``_bus_constraints_from_source`` in ``script/sync_components.py``."""

from __future__ import annotations

from pathlib import Path

from script.sync_components import (  # type: ignore[import-not-found]
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
