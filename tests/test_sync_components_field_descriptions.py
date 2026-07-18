"""Unit tests for the MDX field-description backfill precedence."""

from __future__ import annotations

from script.sync_components import (  # type: ignore[import-not-found]
    _apply_field_descriptions,
    _is_truncated_prefix,
    _parse_config_var_bullets,
)

# The bullet body of esp32.mdx's ``## Advanced Configuration``: a flat bullet with
# a multi-paragraph body (a later ``**Important:**`` note), then a bullet with a
# nested sub-bullet + blockquote.
_ADVANCED_BODY = """\
- **sram1_as_iram** (*Optional*, boolean): Use the SRAM1 memory region as additional IRAM.
  This reclaims memory reserved for the bootloader's DRAM. Defaults to `false`.

  **Important:** This requires a bootloader from ESP-IDF v5.1 or later.

- **signed_ota_verification** (*Optional*, mapping): Enable signed OTA verification.
  - **signing_key** (*Required*, string): The key path.

  > [!NOTE]
  > A note that must not bleed into the description.
"""


def test_truncated_prefix_detects_mid_sentence_head() -> None:
    """A head that breaks mid-sentence and is a prefix of the fuller text."""
    assert _is_truncated_prefix(
        "The global log level. Any log message",
        "The global log level. Any log message with a lower severity will not be shown.",
    )


def test_truncated_prefix_ignores_whitespace_differences() -> None:
    """Prefix matching normalises runs of whitespace on both sides."""
    assert _is_truncated_prefix("A wrapped line", "A   wrapped\nline that keeps going here")


def test_truncated_prefix_false_when_existing_is_complete() -> None:
    """A description ending in terminal punctuation is left alone."""
    assert not _is_truncated_prefix("A complete sentence.", "A complete sentence. More.")
    assert not _is_truncated_prefix("Done!", "Done! And then some.")


def test_truncated_prefix_false_for_list_introducer_colon() -> None:
    """A ``:`` list-introducer is not treated as truncation (its options are sub-bullets)."""
    assert not _is_truncated_prefix("Set the method, can be:", "Set the method, can be: junk tail.")


def test_truncated_prefix_false_when_not_a_prefix_or_not_longer() -> None:
    """Equal text, a non-prefix, and an empty head are all rejected."""
    assert not _is_truncated_prefix("Same text", "Same text")
    assert not _is_truncated_prefix("Totally different", "The global log level")
    assert not _is_truncated_prefix("", "anything at all")


# ``safe_mode.disabled`` as shipped: the schema truncates at "automatically" and
# keeps markdown (`` `true` ``, ``[Ota](...)``); the MDX text is flattened plain.
_SAFE_MODE_DISABLED_SCHEMA = (
    "Set to `true` to disable safe_mode. [Ota](https://esphome.io/components/ota/) automatically"
)
_SAFE_MODE_DISABLED_MDX = (
    "Set to true to disable safe_mode. Ota automatically sets up safe mode; "
    "this allows disabling it if/when it is not wanted."
)


def test_truncated_prefix_matches_markdown_head() -> None:
    """A truncated schema head with markup matches its flattened MDX text."""
    assert _is_truncated_prefix(_SAFE_MODE_DISABLED_SCHEMA, _SAFE_MODE_DISABLED_MDX)


def test_apply_field_descriptions_replaces_truncated_head() -> None:
    """The fuller MDX text supersedes a truncated schema description."""
    entries = [{"key": "level", "description": "The global log level. Any log message"}]
    full = "The global log level. Any log message with a lower severity will not be shown."
    _apply_field_descriptions(
        entries, {"level": full}, docs_url="https://esphome.io/components/logger"
    )
    assert entries[0]["description"] == full
    # The backfill stamps the configuration-variables fragment when unset.
    assert entries[0]["help_link"] == "https://esphome.io/components/logger#configuration-variables"


def test_apply_field_descriptions_replaces_markdown_truncated_head() -> None:
    """A markdown-bearing truncated schema head is superseded by the MDX text."""
    entries = [{"key": "disabled", "description": _SAFE_MODE_DISABLED_SCHEMA}]
    _apply_field_descriptions(entries, {"disabled": _SAFE_MODE_DISABLED_MDX}, docs_url="")
    assert entries[0]["description"] == _SAFE_MODE_DISABLED_MDX


def test_apply_field_descriptions_keeps_complete_description() -> None:
    """A complete description is not overwritten by a longer MDX variant."""
    entries = [{"key": "pin", "description": "The pin to use."}]
    _apply_field_descriptions(
        entries, {"pin": "The pin to use. With a lot of extra detail."}, docs_url=""
    )
    assert entries[0]["description"] == "The pin to use."


def test_apply_field_descriptions_fills_empty_description() -> None:
    """The original empty-fill behaviour is preserved."""
    entries = [{"key": "id", "description": None}]
    _apply_field_descriptions(entries, {"id": "Manually specify the ID."}, docs_url="")
    assert entries[0]["description"] == "Manually specify the ID."


def test_apply_field_descriptions_skips_nested_entries() -> None:
    """A matching key inside a nested entry is not back-filled (flat MDX list)."""
    entries = [
        {
            "key": "esphome",
            "description": "",
            "config_entries": [{"key": "name", "description": ""}],
        }
    ]
    _apply_field_descriptions(entries, {"name": "Leaked nested prose."}, docs_url="")
    assert entries[0]["config_entries"][0]["description"] == ""


def test_parse_bullets_first_paragraph_only_drops_trailing_notes() -> None:
    """With the flag, a blank line ends the field so the ``**Important:**`` note is dropped."""
    fields = _parse_config_var_bullets(_ADVANCED_BODY, first_paragraph_only=True)
    assert fields["sram1_as_iram"].startswith("Use the SRAM1 memory region")
    assert "Important" not in fields["sram1_as_iram"]


def test_parse_bullets_default_keeps_full_prose() -> None:
    """Default (top-level extractor) behaviour joins continuation paragraphs."""
    fields = _parse_config_var_bullets(_ADVANCED_BODY)
    assert "Important" in fields["sram1_as_iram"]


def test_parse_bullets_skips_sub_bullets_and_blockquotes() -> None:
    """A nested sub-bullet isn't a field and a ``> [!NOTE]`` blockquote ends the field."""
    fields = _parse_config_var_bullets(_ADVANCED_BODY, first_paragraph_only=True)
    assert "signing_key" not in fields  # sub-bullet, not a top-level field
    assert "must not bleed" not in fields["signed_ota_verification"]  # blockquote excluded


# max7219digit.mdx's ``scroll_mode``: a sub-bullet whose prose wraps onto a
# deeper-indented line after a blank, then a parent trailing paragraph.
_WRAPPED_SUB_BULLET_BODY = """\
- **scroll_mode** (*Optional*): Set the scroll mode. One of `CONTINUOUS` or `STOP`.

  - `CONTINUOUS`  : Always scrolls and the text repeats, you might need to add some

      separation at the end.

  - `STOP`  : When text is over it waits and scroll is set back to the start.

  This trailing note stays with scroll_mode.

- **scroll_speed** (*Optional*): Set scroll speed. Defaults to `250ms`.
"""


def test_parse_bullets_skips_wrapped_sub_bullet_continuation() -> None:
    """A sub-bullet's deeper-indented wrapped line doesn't leak into the parent field."""
    fields = _parse_config_var_bullets(_WRAPPED_SUB_BULLET_BODY)
    assert "separation at the end" not in fields["scroll_mode"]
    # The parent trailing paragraph (back at the parent indent) is still kept.
    assert "This trailing note stays with scroll_mode." in fields["scroll_mode"]
    assert fields["scroll_speed"].startswith("Set scroll speed")
