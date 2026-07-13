"""Pin the per-line catalog index layout the split writers emit."""

from __future__ import annotations

import orjson

from script._catalog_split import (
    dumps_envelope_entries_per_line,
    dumps_map_entry_per_line,
)

_ENVELOPE = {
    "esphome_version": "2099.1.1",
    "boards": [
        {"id": "aaa", "name": "A", "tags": []},
        {"id": "bbb", "name": "B"},
    ],
}

_MAP = {"board-b": [{"id": "x"}], "board-a": [{"id": "y"}, {"id": "z"}]}


def test_envelope_semantics_match_plain_orjson():
    out = dumps_envelope_entries_per_line(_ENVELOPE, "boards")
    assert orjson.loads(out) == orjson.loads(orjson.dumps(_ENVELOPE, option=orjson.OPT_SORT_KEYS))


def test_envelope_one_entry_per_line():
    lines = dumps_envelope_entries_per_line(_ENVELOPE, "boards").splitlines()
    # {, "boards":[, entry, entry, ],, "esphome_version":…, }
    assert len(lines) == len(_ENVELOPE["boards"]) + 5
    for line, entry in zip(lines[2:4], _ENVELOPE["boards"], strict=True):
        assert orjson.loads(line.rstrip(b",")) == entry


def test_envelope_deterministic_and_clean():
    out = dumps_envelope_entries_per_line(_ENVELOPE, "boards")
    assert out == dumps_envelope_entries_per_line(_ENVELOPE, "boards")
    assert out.endswith(b"}\n")
    assert not any(line != line.rstrip() for line in out.splitlines())


def test_envelope_empty_entries_stay_inline():
    out = dumps_envelope_entries_per_line({"boards": [], "esphome_version": "1"}, "boards")
    assert b'"boards":[]' in out
    assert orjson.loads(out) == {"boards": [], "esphome_version": "1"}


def test_map_semantics_match_plain_orjson():
    out = dumps_map_entry_per_line(_MAP)
    assert orjson.loads(out) == orjson.loads(orjson.dumps(_MAP, option=orjson.OPT_SORT_KEYS))


def test_map_one_key_per_line_sorted():
    lines = dumps_map_entry_per_line(_MAP).splitlines()
    assert len(lines) == len(_MAP) + 2
    assert lines[1].startswith(b'"board-a":')
    assert lines[2].startswith(b'"board-b":')


def test_map_empty():
    assert dumps_map_entry_per_line({}) == b"{}\n"
