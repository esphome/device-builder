"""Unit tests for ``_extract_mdx_description`` in ``script/sync_components.py``."""

from __future__ import annotations

from script.sync_components import (  # type: ignore[import-not-found]
    _extract_mdx_description,
)

# A Figure-first page with no frontmatter ``description:`` — the shape that
# leaked raw ``<Figure …/>`` markup (sensor.ltr501 et al.).
_FIGURE_FIRST_NO_FRONTMATTER = """---
title: "Lite-On Ambient Light & Proximity Sensors"
---

import { Image } from 'astro:assets';
import Figure from '@components/Figure.astro';
import ltr501FullImg from './images/ltr501-full.jpg';

<Figure
  src={ltr501FullImg}
  alt=""
  caption="LTR-501 on a breadboard from Olimex"
  layout="constrained"
/>

The `ltr501` sensor platform allows you to use a range of LiteOn ambient
light and proximity sensors with ESPHome.
"""

# An Image-first page (sprinkler / rc522 shape), also no frontmatter.
_IMAGE_FIRST_NO_FRONTMATTER = """---
title: "Sprinkler Controller"
---

import sprinklerImg from './images/sprinkler.jpg';

<Image
  src={sprinklerImg}
  layout="constrained"
  alt=""
/>

The sprinkler controller component controls sprinkler valves.
"""


def test_multiline_figure_first_returns_following_prose() -> None:
    """A wrapped <Figure …/> before the prose must not leak into the description."""
    result = _extract_mdx_description(_FIGURE_FIRST_NO_FRONTMATTER)
    assert result == (
        "The ltr501 sensor platform allows you to use a range of LiteOn "
        "ambient light and proximity sensors with ESPHome."
    )
    for marker in ("<Figure", "caption=", "src={", "/>", 'alt=""'):
        assert marker not in result


def test_multiline_image_first_returns_following_prose() -> None:
    """A wrapped <Image …/> is skipped the same way as <Figure …/>."""
    result = _extract_mdx_description(_IMAGE_FIRST_NO_FRONTMATTER)
    assert result == "The sprinkler controller component controls sprinkler valves."
    assert "<Image" not in result
    assert "/>" not in result


def test_single_line_figure_first_returns_following_prose() -> None:
    """The pre-existing single-line-tag skip still works (regression guard)."""
    text = """---
title: "X"
---

<Figure src={img} alt="" caption="X" layout="constrained" />

The single-line case still resolves to prose.
"""
    assert _extract_mdx_description(text) == "The single-line case still resolves to prose."


def test_frontmatter_description_strips_instructions_prefix() -> None:
    """Frontmatter wins; the stock "Instructions for setting up " lead is dropped."""
    text = (
        "---\n"
        'description: "Instructions for setting up LTR301, LTR501 sensors with ESPHome."\n'
        'title: "X"\n'
        "---\n\nbody\n"
    )
    assert _extract_mdx_description(text) == "LTR301, LTR501 sensors with ESPHome."


def test_frontmatter_description_flattens_markdown() -> None:
    """A frontmatter description's markdown link / inline code is flattened."""
    text = '---\ndescription: "Use the `foo` platform; see [the docs](https://x/y)."\n---\n\nbody\n'
    assert _extract_mdx_description(text) == "Use the foo platform; see the docs."


def test_only_tags_returns_empty() -> None:
    """No frontmatter description and no prose yields an empty string."""
    text = """---
title: "X"
---

import Figure from '@components/Figure.astro';

<Figure
  src={img}
  alt=""
/>
"""
    assert _extract_mdx_description(text) == ""


def test_inline_jsx_in_prose_line_is_skipped() -> None:
    """The residual-markup guard drops a prose line carrying inline JSX."""
    # An inline <Figure …/> mid-line doesn't start the line, so the prefix
    # skip can't catch it; the "/>"/"={" guard rejects the paragraph instead.
    text = (
        '---\ntitle: "X"\n---\n\n'
        'This sensor uses <Figure src={img} alt="" /> for wiring.\n\n'
        "The clean follow-up paragraph wins.\n"
    )
    assert _extract_mdx_description(text) == "The clean follow-up paragraph wins."
