"""CodSpeed benchmarks for device-builder hot paths.

Mirrors the layout used in ``aioesphomeapi``, ``habluetooth``, and
``bleak-esphome``: each test file holds ``benchmark`` fixture-driven
regressions that CodSpeed runs under instrumentation in CI. We keep
the benchmarks separate from the unit-test suite so the regular
``uv run pytest`` path stays fast — the benchmarks only run when
explicitly targeted (``pytest tests/benchmarks --codspeed``).
"""
