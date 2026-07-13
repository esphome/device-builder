"""Pin the per-line catalog index layout the split writers emit."""

from __future__ import annotations

from pathlib import Path

import orjson

from script._catalog_split import (
    dumps_envelope_entries_per_line,
    dumps_map_entry_per_line,
)

_DEFINITIONS_DIR = Path(__file__).parent.parent / "esphome_device_builder" / "definitions"

_ENVELOPE = {
    "esphome_version": "2099.1.1",
    "boards": [
        {"id": "aaa", "name": "A", "tags": []},
        {"id": "bbb", "name": "B"},
    ],
}

_MAP = {"board-b": [{"id": "x"}], "board-a": [{"id": "y"}, {"id": "z"}]}

# Automations-shaped envelope: one scalar plus several entry lists.
_MULTI_ENVELOPE = {
    "esphome_schema_version": "2099.1.1",
    "triggers": [{"id": "t1"}, {"id": "t2"}],
    "actions": [{"id": "a1"}],
    "filters": [],
}


def test_envelope_semantics_match_plain_orjson():
    out = dumps_envelope_entries_per_line(_ENVELOPE, ("boards",))
    assert orjson.loads(out) == orjson.loads(orjson.dumps(_ENVELOPE, option=orjson.OPT_SORT_KEYS))


def test_envelope_one_entry_per_line():
    lines = dumps_envelope_entries_per_line(_ENVELOPE, ("boards",)).splitlines()
    # 5 envelope lines: `{`, `"boards":[`, `],`, `"esphome_version":…`, `}` —
    # plus one line per entry between the brackets.
    assert len(lines) == len(_ENVELOPE["boards"]) + 5
    for line, entry in zip(lines[2:4], _ENVELOPE["boards"], strict=True):
        assert orjson.loads(line.rstrip(b",")) == entry


def test_envelope_deterministic_and_clean():
    out = dumps_envelope_entries_per_line(_ENVELOPE, ("boards",))
    assert out == dumps_envelope_entries_per_line(_ENVELOPE, ("boards",))
    assert out.endswith(b"}\n")
    assert not any(line != line.rstrip() for line in out.splitlines())


def test_envelope_empty_entries_stay_inline():
    out = dumps_envelope_entries_per_line({"boards": [], "esphome_version": "1"}, ("boards",))
    assert b'"boards":[]' in out
    assert orjson.loads(out) == {"boards": [], "esphome_version": "1"}


def test_multi_key_envelope_semantics_match_plain_orjson():
    out = dumps_envelope_entries_per_line(_MULTI_ENVELOPE, ("triggers", "actions", "filters"))
    assert orjson.loads(out) == orjson.loads(
        orjson.dumps(_MULTI_ENVELOPE, option=orjson.OPT_SORT_KEYS)
    )


def test_multi_key_envelope_expands_each_list():
    keys = ("triggers", "actions", "filters")
    lines = dumps_envelope_entries_per_line(_MULTI_ENVELOPE, keys).splitlines()
    # `{`, `}`, one scalar line, an empty list inline, and per non-empty
    # list two bracket lines plus one line per entry.
    non_empty = [k for k in keys if _MULTI_ENVELOPE[k]]
    entries = sum(len(_MULTI_ENVELOPE[k]) for k in keys)
    assert len(lines) == entries + 2 * len(non_empty) + 1 + 1 + 2
    assert b'"actions":[' in lines
    assert b'"triggers":[' in lines
    assert lines[1] == b'"actions":['
    assert orjson.loads(lines[2].rstrip(b",")) == {"id": "a1"}
    # The empty list and the scalar stay inline on their own lines.
    assert b'"filters":[],' in lines
    assert any(line.startswith(b'"esphome_schema_version":') for line in lines)


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


def test_components_index_is_one_entry_per_line():
    """The committed index keeps each component on its own line (merge-conflict shape)."""
    raw = (_DEFINITIONS_DIR / "components.index.json").read_bytes()
    payload = orjson.loads(raw)
    components = payload["components"]
    assert len(raw.splitlines()) == len(components) + 5
    entry_lines = raw.splitlines()[2 : 2 + len(components)]
    for line, entry in zip(entry_lines, components, strict=True):
        assert orjson.loads(line.rstrip(b",")) == entry


def test_automations_index_is_one_entry_per_line():
    """The committed index keeps each automation entry on its own line, per sub-catalog."""
    raw = (_DEFINITIONS_DIR / "automations.index.json").read_bytes()
    payload = orjson.loads(raw)
    lines = raw.splitlines()
    list_keys = [k for k, v in payload.items() if isinstance(v, list)]
    non_empty = [k for k in list_keys if payload[k]]
    entries = sum(len(payload[k]) for k in list_keys)
    # Scalars and empty lists land inline, one line each; each non-empty
    # list adds two bracket lines around its per-line entries.
    inline_lines = len(payload) - len(non_empty)
    assert len(lines) == entries + 2 * len(non_empty) + inline_lines + 2
    for key in non_empty:
        start = lines.index(orjson.dumps(key) + b":[") + 1
        entry_lines = lines[start : start + len(payload[key])]
        for line, entry in zip(entry_lines, payload[key], strict=True):
            assert orjson.loads(line.rstrip(b",")) == entry
