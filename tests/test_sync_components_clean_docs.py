"""Unit tests for ``clean_docs`` in ``script/sync_components.py``."""

from __future__ import annotations

from script.sync_components import clean_docs  # type: ignore[import-not-found]


def test_clean_docs_treats_literal_none_as_empty() -> None:
    """ESPHome's schema dump writes a missing docstring as the string "None"."""
    assert clean_docs("None").text == ""
    assert clean_docs("  None  ").text == ""


def test_clean_docs_none_body_keeps_see_also_footer() -> None:
    """A "None" body with a real See-also footer drops the body, keeps name/url."""
    cleaned = clean_docs(
        "None\n\n*See also: [ATM90E32 Power Sensor](https://esphome.io/components/sensor/atm90e32)*"
    )
    assert cleaned.text == ""
    assert cleaned.name == "ATM90E32 Power Sensor"
    assert cleaned.url == "https://esphome.io/components/sensor/atm90e32"


def test_clean_docs_keeps_text_starting_with_none() -> None:
    """Only an exact "None" body is dropped — a real description is preserved."""
    assert clean_docs("None of the channels are enabled by default.").text == (
        "None of the channels are enabled by default."
    )


def test_clean_docs_passes_through_a_normal_description() -> None:
    """A regular description round-trips with its type prefix stripped."""
    assert clean_docs("**string**: The friendly name.").text == "The friendly name."
