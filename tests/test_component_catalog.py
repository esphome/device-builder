"""Unit tests for ``load_component_catalog`` in ``script/_component_catalog.py``."""

from __future__ import annotations

import json
from pathlib import Path

from script._component_catalog import load_component_catalog  # type: ignore[import-not-found]


def _write(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_merges_body_over_index_entry(tmp_path: Path) -> None:
    bodies = tmp_path / "components"
    bodies.mkdir()
    _write(
        tmp_path / "index.json",
        {"components": [{"id": "sensor.dht", "title": "DHT"}]},
    )
    _write(bodies / "sensor.dht.json", {"config_entries": [{"key": "pin"}]})

    result = load_component_catalog(tmp_path / "index.json", bodies)

    assert result == {
        "sensor.dht": {"id": "sensor.dht", "title": "DHT", "config_entries": [{"key": "pin"}]},
    }


def test_keeps_index_entry_without_body(tmp_path: Path) -> None:
    bodies = tmp_path / "components"
    bodies.mkdir()
    _write(tmp_path / "index.json", {"components": [{"id": "sensor.dht", "title": "DHT"}]})

    result = load_component_catalog(tmp_path / "index.json", bodies)

    assert result == {"sensor.dht": {"id": "sensor.dht", "title": "DHT"}}


def test_skips_entries_without_id(tmp_path: Path) -> None:
    bodies = tmp_path / "components"
    bodies.mkdir()
    _write(
        tmp_path / "index.json",
        {"components": [{"title": "no id"}, {"id": "sensor.dht"}]},
    )

    result = load_component_catalog(tmp_path / "index.json", bodies)

    assert list(result) == ["sensor.dht"]
