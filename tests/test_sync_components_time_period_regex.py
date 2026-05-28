r"""
Tests for ``_looks_like_time_period_default``.

The underlying regex was rewritten after a CodeQL ReDoS alert
flagged the old ``(\d+\s*\w+)*`` repeating group (the ``\d+`` /
``\w+`` overlap caused exponential backtracking on inputs
starting with a valid prefix followed by many ambiguous trailing
characters). The new shape sticks to the fixed unit alternation
so the regex matches in linear time.
"""

from __future__ import annotations

import time

import pytest

from script.sync_components import (  # type: ignore[import-not-found]
    _looks_like_time_period_default,
)


@pytest.mark.parametrize(
    "value",
    [
        "60s",
        "5min",
        "1h30s",
        "500ms",
        "1.5h",
        "1d2h30min45s",
        "100ns",
        "50us",
        # The caller pre-strips whitespace; pass the stripped form.
        "60s",
    ],
)
def test_canonical_time_period_strings_match(value: str) -> None:
    assert _looks_like_time_period_default(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "",
        "abc",
        # Number without a unit.
        "60",
        # Unit without a number.
        "h",
        # Unknown unit.
        "60x",
        # Trailing number with no unit after.
        "60s30",
        "1h30",
        # Non-string falls through ``isinstance`` guard.
        42,
        None,
    ],
)
def test_non_time_period_inputs_do_not_match(value: object) -> None:
    assert _looks_like_time_period_default(value) is False


def test_pathological_input_runs_in_linear_time() -> None:
    """The ``9s9`` + many ``00`` shape CodeQL flagged must not backtrack."""
    pathological = "9s9" + "00" * 200
    start = time.perf_counter()
    matched = _looks_like_time_period_default(pathological)
    elapsed_us = (time.perf_counter() - start) * 1e6
    assert matched is False
    # Generous bound; the old regex would have taken seconds on
    # this input. A linear-time match completes in microseconds.
    assert elapsed_us < 50_000, f"regex took {elapsed_us:.0f}us — possible ReDoS regression"
