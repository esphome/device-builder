"""Pure-Python unit tests for the release-notes generator.

Only exercises the parser/renderer halves — anything that calls into
PyGithub is integration-tested implicitly via a dry run from the repo
maintainer when the action lands.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def gen():
    """Import the script as a module despite its dotted parent path."""
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    try:
        import generate_notes

        return generate_notes
    finally:
        sys.path.pop(0)


def test_parse_pyproject_extracts_pinned_version(gen):
    text = """
[project]
name = "esphome-device-builder"
dependencies = [
  "esphome-device-builder-frontend==0.1.25",
  "aiohttp>=3.9.0",
]
"""
    assert gen.parse_pyproject_frontend_version(text, "esphome-device-builder-frontend") == "0.1.25"


def test_parse_pyproject_returns_none_for_range_pin(gen):
    text = """
[project]
dependencies = [
  "esphome-device-builder-frontend>=0.1.0",
]
"""
    assert gen.parse_pyproject_frontend_version(text, "esphome-device-builder-frontend") is None


def test_parse_pyproject_returns_none_when_dep_missing(gen):
    text = """
[project]
dependencies = ["aiohttp>=3.9.0"]
"""
    assert gen.parse_pyproject_frontend_version(text, "esphome-device-builder-frontend") is None


def test_parse_pyproject_handles_invalid_toml(gen):
    assert (
        gen.parse_pyproject_frontend_version(
            "this is not toml [", "esphome-device-builder-frontend"
        )
        is None
    )


# A realistic frontend release body; mirrors what release-drafter emits in the
# frontend repo, including the bare ``#NNN`` PR refs and the trailing
# Contributors section we want to ignore.
_FRONTEND_BODY = """\
## 🚀 New features

- Add awesome thing (by @alice in #99)

## 🐛 Bug fixes

- Stop the editor from eating the trailing newline (by @bob in #100)
- Restore something else (by `@carol` in #101)

## ⬆️ Dependencies / CI

- Bump vue from 3.1.2 to 3.1.3 (by @dependabot[bot] in #102)

## :bow: Contributors

@alice, @bob, @carol
"""


def test_parse_frontend_release_body_buckets_known_categories(gen):
    by_cat = gen.parse_frontend_release_body(
        _FRONTEND_BODY,
        category_titles=["🚀 New features", "🐛 Bug fixes", "⬆️ Dependencies / CI"],
        skip_titles={"⬆️ Dependencies / CI"},
        frontend_repo_full_name="esphome/device-builder-frontend",
    )

    new_features = by_cat["🚀 New features"]
    assert [c["number"] for c in new_features] == [99]
    assert new_features[0]["title"] == "Add awesome thing"
    assert new_features[0]["author"] == "alice"
    assert new_features[0]["is_frontend"] is True
    assert new_features[0]["url"] == "https://github.com/esphome/device-builder-frontend/pull/99"

    bug_fixes = by_cat["🐛 Bug fixes"]
    assert [c["number"] for c in bug_fixes] == [100, 101]
    # Backtick-wrapped author handles in cross-repo includes are normalised away.
    assert bug_fixes[1]["author"] == "carol"


def test_parse_frontend_release_body_skips_dependencies_section(gen):
    by_cat = gen.parse_frontend_release_body(
        _FRONTEND_BODY,
        category_titles=["🚀 New features", "🐛 Bug fixes", "⬆️ Dependencies / CI"],
        skip_titles={"⬆️ Dependencies / CI"},
        frontend_repo_full_name="esphome/device-builder-frontend",
    )
    assert "⬆️ Dependencies / CI" not in by_cat


def test_parse_frontend_release_body_drops_bullets_under_unknown_heading(gen):
    body = """\
## Some made-up heading

- Untracked work (by @alice in #1)

## 🐛 Bug fixes

- A real fix (by @bob in #2)
"""
    by_cat = gen.parse_frontend_release_body(
        body,
        category_titles=["🐛 Bug fixes"],
        skip_titles=set(),
        frontend_repo_full_name="esphome/device-builder-frontend",
    )
    assert list(by_cat) == ["🐛 Bug fixes"]
    assert by_cat["🐛 Bug fixes"][0]["number"] == 2


def test_parse_frontend_release_body_drops_bullets_before_first_heading(gen):
    body = """\
- Stray bullet (by @alice in #1)

## 🐛 Bug fixes

- Categorised fix (by @bob in #2)
"""
    by_cat = gen.parse_frontend_release_body(
        body,
        category_titles=["🐛 Bug fixes"],
        skip_titles=set(),
        frontend_repo_full_name="esphome/device-builder-frontend",
    )
    assert by_cat["🐛 Bug fixes"][0]["number"] == 2
    assert all(c["number"] != 1 for changes in by_cat.values() for c in changes)


def test_parse_frontend_release_body_ignores_contributors_section(gen):
    by_cat = gen.parse_frontend_release_body(
        _FRONTEND_BODY,
        category_titles=["🚀 New features", "🐛 Bug fixes"],
        skip_titles=set(),
        frontend_repo_full_name="esphome/device-builder-frontend",
    )
    # The contributors heading isn't a known category title, so its bullets
    # — if any — would be dropped along with the heading itself.
    assert ":bow:" not in str(by_cat)


def test_render_change_line_backend_uses_bare_number(gen):
    line = gen.render_change_line(
        "- #$NUMBER - $TITLE (@$AUTHOR)",
        {
            "title": "Fix something",
            "author": "alice",
            "number": 42,
            "url": "https://github.com/esphome/device-builder/pull/42",
            "is_frontend": False,
        },
    )
    # Backend lines keep ``#42`` literal — GitHub auto-links them to the
    # surrounding repo at render time.
    assert line == "- #42 - Fix something (@alice)"


def test_render_change_line_frontend_emits_explicit_link(gen):
    line = gen.render_change_line(
        "- #$NUMBER - $TITLE (@$AUTHOR)",
        {
            "title": "Fix something",
            "author": "alice",
            "number": 42,
            "url": "https://github.com/esphome/device-builder-frontend/pull/42",
            "is_frontend": True,
        },
    )
    # Frontend lines must hard-code the URL: the same ``#42`` would
    # otherwise auto-resolve against the backend repo. The ``frontend``
    # prefix on the link text disambiguates which repo the PR lives in.
    assert line == (
        "- [frontend#42](https://github.com/esphome/device-builder-frontend/pull/42) - "
        "Fix something (@alice)"
    )


def test_build_release_notes_renders_backend_and_frontend_together(gen):
    config = {
        "change-template": "- #$NUMBER - $TITLE (@$AUTHOR)",
        "template": "## What's changed\n\n$CHANGES\n",
        "categories": [
            {"title": "🐛 Bug fixes", "labels": ["bugfix"]},
            {"title": "⬆️ Dependencies / CI", "labels": ["dependencies"], "collapse-after": 1},
        ],
    }
    by_category = {
        "🐛 Bug fixes": [
            {
                "title": "Fix backend",
                "author": "alice",
                "number": 1,
                "url": "https://github.com/owner/backend/pull/1",
                "is_frontend": False,
            },
            {
                "title": "Fix frontend",
                "author": "bob",
                "number": 200,
                "url": "https://github.com/owner/frontend/pull/200",
                "is_frontend": True,
            },
        ],
    }
    notes = gen.build_release_notes(
        config=config,
        by_category=by_category,
        previous_tag="0.1.24",
        repo_url="https://github.com/owner/backend",
    )
    assert "## What's changed" in notes
    assert "_Changes since [0.1.24](https://github.com/owner/backend/releases/tag/0.1.24)_" in notes
    assert "### 🐛 Bug fixes" in notes
    assert "- #1 - Fix backend (@alice)" in notes
    assert (
        "- [frontend#200](https://github.com/owner/frontend/pull/200) - Fix frontend (@bob)"
        in notes
    )
    # Empty categories are dropped entirely — no Dependencies / CI section here.
    assert "Dependencies" not in notes


def test_build_release_notes_collapses_long_dependency_lists(gen):
    config = {
        "change-template": "- #$NUMBER - $TITLE (@$AUTHOR)",
        "template": "## What's changed\n\n$CHANGES\n",
        "categories": [
            {"title": "⬆️ Dependencies / CI", "labels": ["dependencies"], "collapse-after": 1},
        ],
    }
    by_category = {
        "⬆️ Dependencies / CI": [
            {
                "title": f"Bump foo {i}",
                "author": "dep",
                "number": i,
                "url": f"https://github.com/owner/backend/pull/{i}",
                "is_frontend": False,
            }
            for i in range(3)
        ],
    }
    notes = gen.build_release_notes(
        config=config,
        by_category=by_category,
        previous_tag=None,
        repo_url="https://github.com/owner/backend",
    )
    assert "<details>" in notes
    assert "<summary>3 changes</summary>" in notes
    assert "</details>" in notes


def test_categorise_backend_pr_picks_first_match(gen):
    cfgs = [
        {"title": "🚀 New features", "labels": ["new-feature", "enhancement"]},
        {"title": "🐛 Bug fixes", "labels": ["bugfix"]},
    ]
    assert gen.categorise_backend_pr({"bugfix"}, cfgs) == "🐛 Bug fixes"
    assert gen.categorise_backend_pr({"enhancement"}, cfgs) == "🚀 New features"
    assert gen.categorise_backend_pr({"unrelated"}, cfgs) is None


def test_version_tuple_orders_lexicographically(gen):
    assert gen._version_tuple("0.1.21") < gen._version_tuple("0.1.22")
    assert gen._version_tuple("0.2.0") > gen._version_tuple("0.1.99")
