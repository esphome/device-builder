"""Bootstrap pairing-key generation + comparison."""

from __future__ import annotations

import pytest

from esphome_device_builder.helpers.pairing_psk import (
    _PSK_ALPHABET,
    generate_pairing_psk,
    pairing_psk_matches,
)


def test_generate_shape() -> None:
    psk = generate_pairing_psk()
    groups = psk.split("-")
    assert len(groups) == 4
    assert all(len(g) == 4 for g in groups)
    assert all(c in _PSK_ALPHABET for g in groups for c in g)


def test_generate_is_unique() -> None:
    assert generate_pairing_psk() != generate_pairing_psk()


def test_alphabet_excludes_ambiguous_glyphs() -> None:
    assert not set("0O1ILU") & set(_PSK_ALPHABET)


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
    assert pairing_psk_matches("ABCD-EFGH-JKMN-PQRS", presented)


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
    assert not pairing_psk_matches("ABCD-EFGH-JKMN-PQRS", presented)
