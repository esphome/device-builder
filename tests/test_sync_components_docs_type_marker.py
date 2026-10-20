"""Type inference for untyped config vars: default shape versus docs marker."""

from __future__ import annotations

from pathlib import Path

import pytest

from script.sync_components import _convert_config_vars  # type: ignore[import-not-found]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(
            {"key": "Optional", "default": "60s", "docs": "**int**: Polling interval."},
            "time_period",
            id="time_default_beats_int_marker",
        ),
        pytest.param(
            {"key": "Optional", "default": "60s", "docs": "**[Time](https://x)**: Interval."},
            "time_period",
            id="time_default_agrees_with_time_marker",
        ),
        pytest.param(
            {"key": "Optional", "default": "5", "docs": "**int**: Sample count."},
            "integer",
            id="plain_default_keeps_int_marker",
        ),
        pytest.param(
            {"key": "Optional", "docs": "**int**: Sample count."},
            "integer",
            id="no_default_keeps_int_marker",
        ),
    ],
)
def test_untyped_field_type(tmp_path: Path, raw: dict[str, object], expected: str) -> None:
    (entry,) = _convert_config_vars({"config_vars": {"update_interval": raw}}, tmp_path)

    assert entry["type"] == expected
