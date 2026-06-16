"""Tests for the prerelease docs-host canonicalization in the catalog emit."""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).parent.parent / "script"
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import sync_components  # noqa: E402


def test_canonicalize_rewrites_beta_and_next_hosts() -> None:
    """beta./next. esphome.io hosts collapse to the canonical host; others untouched."""
    tree = {
        "docs_url": "https://beta.esphome.io/components/api",
        "description": "An [Action](https://next.esphome.io/automations/actions#x) here.",
        "entries": [
            {"help_link": "https://beta.esphome.io/components/wifi#ap"},
            {"keep": "https://esphome.io/components/logger"},
            {"unrelated": "https://schema.esphome.io/2026.6.0b3/schema.zip"},
        ],
    }
    out = sync_components._canonicalize_docs_hosts(tree)
    assert out["docs_url"] == "https://esphome.io/components/api"
    assert out["description"] == "An [Action](https://esphome.io/automations/actions#x) here."
    assert out["entries"][0]["help_link"] == "https://esphome.io/components/wifi#ap"
    # Canonical and non-docs esphome.io hosts are left alone.
    assert out["entries"][1]["keep"] == "https://esphome.io/components/logger"
    assert out["entries"][2]["unrelated"] == "https://schema.esphome.io/2026.6.0b3/schema.zip"


def test_canonicalize_preserves_non_string_leaves() -> None:
    """Non-string values pass through unchanged."""
    tree = {"multi_conf": True, "count": 3, "items": [1, None, "https://beta.esphome.io/x"]}
    out = sync_components._canonicalize_docs_hosts(tree)
    assert out == {"multi_conf": True, "count": 3, "items": [1, None, "https://esphome.io/x"]}
