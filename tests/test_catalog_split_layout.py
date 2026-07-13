"""Pin the per-line catalog index layout the split writers emit."""

from __future__ import annotations

from pathlib import Path

import orjson
import pytest

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


def _assert_entries_per_line(raw: bytes, payload: dict) -> None:
    """Assert *raw* holds *payload* with every non-empty list one entry per line."""
    lines = raw.splitlines()
    non_empty = [k for k, v in payload.items() if isinstance(v, list) and v]
    entries = sum(len(payload[k]) for k in non_empty)
    # Scalars and empty lists land inline, one line each; each non-empty
    # list adds two bracket lines around its per-line entries.
    inline_lines = len(payload) - len(non_empty)
    assert len(lines) == entries + 2 * len(non_empty) + inline_lines + 2
    for key in non_empty:
        start = lines.index(orjson.dumps(key) + b":[") + 1
        entry_lines = lines[start : start + len(payload[key])]
        for line, entry in zip(entry_lines, payload[key], strict=True):
            assert orjson.loads(line.rstrip(b",")) == entry


def test_envelope_semantics_match_plain_orjson():
    out = dumps_envelope_entries_per_line(_ENVELOPE)
    assert orjson.loads(out) == orjson.loads(orjson.dumps(_ENVELOPE, option=orjson.OPT_SORT_KEYS))


def test_envelope_one_entry_per_line():
    lines = dumps_envelope_entries_per_line(_ENVELOPE).splitlines()
    # 5 envelope lines: `{`, `"boards":[`, `],`, `"esphome_version":…`, `}` —
    # plus one line per entry between the brackets.
    assert len(lines) == len(_ENVELOPE["boards"]) + 5
    for line, entry in zip(lines[2:4], _ENVELOPE["boards"], strict=True):
        assert orjson.loads(line.rstrip(b",")) == entry


def test_envelope_deterministic_and_clean():
    out = dumps_envelope_entries_per_line(_ENVELOPE)
    assert out == dumps_envelope_entries_per_line(_ENVELOPE)
    assert out.endswith(b"}\n")
    assert not any(line != line.rstrip() for line in out.splitlines())


def test_envelope_empty_entries_stay_inline():
    out = dumps_envelope_entries_per_line({"boards": [], "esphome_version": "1"})
    assert b'"boards":[]' in out
    assert orjson.loads(out) == {"boards": [], "esphome_version": "1"}


def test_multi_list_envelope_layout():
    """Every non-empty list expands per-line; scalars and empty lists stay inline."""
    assert dumps_envelope_entries_per_line(_MULTI_ENVELOPE) == (
        b"{\n"
        b'"actions":[\n'
        b'{"id":"a1"}\n'
        b"],\n"
        b'"esphome_schema_version":"2099.1.1",\n'
        b'"filters":[],\n'
        b'"triggers":[\n'
        b'{"id":"t1"},\n'
        b'{"id":"t2"}\n'
        b"]\n"
        b"}\n"
    )


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


@pytest.mark.parametrize(
    "index_name",
    ["components.index.json", "automations.index.json"],
)
def test_committed_index_is_one_entry_per_line(index_name: str):
    """The committed index keeps each entry on its own line (merge-conflict shape)."""
    raw = (_DEFINITIONS_DIR / index_name).read_bytes()
    _assert_entries_per_line(raw, orjson.loads(raw))
