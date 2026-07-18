"""Unit tests for the description tidy-up pass (fences + dangling list-introducers)."""

from __future__ import annotations

import time

import pytest

from script.sync_components import (  # type: ignore[import-not-found]
    _tidy_all_descriptions,
    _tidy_description,
)

# --- Fenced-code removal -----------------------------------------------------


def test_strips_triple_fenced_code_mid_sentence() -> None:
    """A ```json``` example between two clauses is removed, the prose stays."""
    text = (
        "When set to `true`, state is published as one JSON object. "
        'Example: ```json { "state": "open" } ``` '
        "When `false`, values are published separately. Defaults to `false`."
    )
    out = _tidy_description(text)
    assert out == (
        "When set to `true`, state is published as one JSON object. "
        "When `false`, values are published separately. Defaults to `false`."
    )
    assert '{ "state"' not in out  # fenced body gone
    assert "`true`" in out and "`false`" in out  # inline code preserved


def test_strips_language_tagged_double_fence() -> None:
    """An ``yaml ... `` inline example block is removed with its ``Example:`` intro."""
    text = (
        "The ID of a led widget configured in LVGL, which will reflect the state "
        "of the light. Example: ``yaml light: widget: led_id name: LVGL light ``."
    )
    out = _tidy_description(text)
    assert out == (
        "The ID of a led widget configured in LVGL, which will reflect the state of the light."
    )


def test_strips_unterminated_trailing_fence() -> None:
    """An opening fence whose body was on excluded sub-lines is dropped from the tail."""
    text = (
        "Any command sent to the Modbus Select immediately updates the reported "
        "state. Defaults to false. ```yaml."
    )
    out = _tidy_description(text)
    assert out == (
        "Any command sent to the Modbus Select immediately updates the reported "
        "state. Defaults to false."
    )


def test_strips_untagged_triple_fence_with_brace_body() -> None:
    """A plain ```{...}``` fence with no language tag (body starts with `{`) is removed."""
    text = 'The payload shape. Example: ```{ "state": "open" }``` See the notes.'
    assert _tidy_description(text) == "The payload shape. See the notes."


def test_no_catastrophic_backtracking_on_whitespace_run_and_open_fence() -> None:
    """A long whitespace run before an unterminated fence tidies quickly (ReDoS guard)."""
    payload = "State." + " " * 4000 + "```"
    start = time.perf_counter()
    out = _tidy_description(payload)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0  # exponential backtracking would take minutes here
    assert out.startswith("State.")


def test_strips_cpp_fence_keeps_surrounding_prose() -> None:
    """A ```c++``` formula block is removed but the explanation around it stays."""
    text = (
        "The compensated ambient temperature is calculated as follows: "
        "```c++ T = T_Ambient + offset ``` "
        "Where slope and offset are the values set with this command."
    )
    out = _tidy_description(text)
    assert "```" not in out and "T_Ambient" not in out
    assert out.startswith("The compensated ambient temperature is calculated as follows")
    assert out.endswith("Where slope and offset are the values set with this command.")


# --- Dangling list-introducer trimming --------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("The type of interrupt to use. One of:", "The type of interrupt to use."),
        ("Operating mode. One of:", "Operating mode."),
        (
            "Output clock speed. Defaults to `20MHZ`. One of:",
            "Output clock speed. Defaults to `20MHZ`.",
        ),
        (
            "Sets the analog gain. Defaults to 1X. Must be one of.",
            "Sets the analog gain. Defaults to 1X.",
        ),
        ("The metadata field to report. One of.", "The metadata field to report."),
        ("The nrf-sdk version. One of.", "The nrf-sdk version."),
        (
            "The mode to use. Must be one of the following values:",
            "The mode to use.",
        ),
    ],
)
def test_trims_dangling_list_introducer(text: str, expected: str) -> None:
    """A trailing bare list-introducer (``One of:`` / ``one of.``) is dropped."""
    assert _tidy_description(text) == expected


# --- Preservation guards (must NOT change) ----------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # inline double-backtick code is not a fenced block
        "Invert the logical level. ``true`` swaps high/low so an active-low button reads active.",
        # "one of" inside a real sentence, not a dangling introducer
        "Pick one of the modes.",
        "This is one of the required fields.",
        "The ID of the descriptor to set the value of.",
        "Must be set to one of the supported values.",
        # a real sentence that happens to end in a colon (list of items follows in the UI)
        "It provides support for the following microcontrollers, commonly used in Tuya devices:",
        "UART usually consists of 2 pins:",
        "Only on ESP32, it is possible to specify run duration according to the wakeup reason:",
        # already-clean prose
        "The global log level. Any log message with a lower severity will not be shown.",
        "The pin to use.",
    ],
)
def test_leaves_legitimate_descriptions_unchanged(text: str) -> None:
    """Inline code, real ``one of`` sentences, and legit trailing colons are preserved."""
    assert _tidy_description(text) == text


def test_preserves_ellipsis_when_a_fence_is_stripped() -> None:
    """A legitimate ``...`` survives the terminal-punctuation collapse after a strip."""
    assert _tidy_description("Loading state... ```yaml x: 1``` then done.") == (
        "Loading state... then done."
    )


def test_empty_and_none_safe() -> None:
    """Empty input is returned as-is without error."""
    assert _tidy_description("") == ""


def test_never_blanks_a_description() -> None:
    """A description that is only an introducer falls back to the original, never empty."""
    assert _tidy_description("One of:") == "One of:"


# --- Whole-tree walker -------------------------------------------------------


def test_tidy_all_descriptions_walks_nested_config_entries() -> None:
    """The walker rewrites descriptions at every depth and counts the changes."""
    catalog = [
        {
            "id": "demo",
            "description": "A demo component. One of:",
            "config_entries": [
                {"key": "mode", "description": "Operating mode. One of:"},
                {
                    "key": "group",
                    "description": "A clean container description.",
                    "config_entries": [
                        {"key": "inner", "description": "Inner field. Must be one of."},
                    ],
                },
            ],
        }
    ]
    changed = _tidy_all_descriptions(catalog)
    assert changed == 3
    assert catalog[0]["description"] == "A demo component."
    assert catalog[0]["config_entries"][0]["description"] == "Operating mode."
    assert catalog[0]["config_entries"][1]["description"] == "A clean container description."
    assert catalog[0]["config_entries"][1]["config_entries"][0]["description"] == "Inner field."


def test_tidy_all_descriptions_noop_on_clean_tree() -> None:
    """A tree with no garbled descriptions is left untouched (zero changes)."""
    catalog = [
        {
            "id": "x",
            "description": "Clean.",
            "config_entries": [{"key": "p", "description": "A pin."}],
        }
    ]
    assert _tidy_all_descriptions(catalog) == 0
