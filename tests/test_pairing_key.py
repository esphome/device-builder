"""Bootstrap pairing-key generation + comparison."""

from __future__ import annotations

import pytest

from esphome_device_builder.helpers.pairing_key import (
    _KEY_ALPHABET,
    generate_pairing_key,
    pairing_key_matches,
)


def test_generate_shape() -> None:
    key = generate_pairing_key()
    groups = key.split("-")
    assert len(groups) == 4
    assert all(len(g) == 4 for g in groups)
    assert all(c in _KEY_ALPHABET for g in groups for c in g)


def test_generate_is_unique() -> None:
    assert generate_pairing_key() != generate_pairing_key()


def test_alphabet_excludes_ambiguous_glyphs() -> None:
    assert not set("0O1ILU") & set(_KEY_ALPHABET)


@pytest.mark.parametrize(
    "presented",
    [
        "ABCD-EFGH-JKMN-PQRS",
        "abcd-efgh-jkmn-pqrs",
        "ABCDEFGHJKMNPQRS",
        "  abcd efgh jkmn pqrs  ",
        "abcd_efgh.jkmn:pqrs",
    ],
)
def test_matches_ignores_case_and_separators(presented: str) -> None:
    assert pairing_key_matches("ABCD-EFGH-JKMN-PQRS", presented)


@pytest.mark.parametrize(
    "presented",
    [
        None,
        "",
        "----",
        "ABCD-EFGH-JKMN-PQRT",
        "ABCD-EFGH-JKMN",
        "ABCD-EFGH-JKMN-PQRS-TVWX",
    ],
)
def test_mismatches(presented: str | None) -> None:
    assert not pairing_key_matches("ABCD-EFGH-JKMN-PQRS", presented)
