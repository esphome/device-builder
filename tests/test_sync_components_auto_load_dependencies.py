"""AUTO_LOADed components' ``DEPENDENCIES`` join the catalog dependency list."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import script.sync_components as sc
from script.sync_components import _auto_loaded_dependencies


class _FakeLoader:
    """Stand-in for ``esphome.loader``: manifests keyed by name / (domain, name)."""

    def __init__(self, components: dict[str, Any], platforms: dict[tuple[str, str], Any]) -> None:
        self._components = components
        self._platforms = platforms

    def get_component(self, name: str) -> Any:
        return self._components.get(name)

    def get_platform(self, domain: str, name: str) -> Any:
        return self._platforms.get((domain, name))


def _manifest(auto_load: list[str] | None = None, dependencies: list[str] | None = None) -> Any:
    return SimpleNamespace(auto_load=auto_load or [], dependencies=dependencies or [])


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    _auto_loaded_dependencies.cache_clear()


def _install(monkeypatch: pytest.MonkeyPatch, loader: _FakeLoader) -> None:
    monkeypatch.setattr(sc, "_get_esphome_loader", lambda: loader)


def test_platform_auto_load_contributes_its_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The climate_ir shape: platform auto-loads a base whose DEPENDENCIES count."""
    loader = _FakeLoader(
        components={"climate_ir": _manifest(dependencies=["remote_transmitter"])},
        platforms={("climate", "climate_ir_lg"): _manifest(auto_load=["climate_ir"])},
    )
    _install(monkeypatch, loader)
    assert _auto_loaded_dependencies("climate", "climate_ir_lg") == ("remote_transmitter",)


def test_closure_is_transitive_and_cycle_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    loader = _FakeLoader(
        components={
            "a": _manifest(auto_load=["b"], dependencies=["dep_a"]),
            "b": _manifest(auto_load=["a"], dependencies=["dep_b"]),
        },
        platforms={("sensor", "leaf"): _manifest(auto_load=["a"])},
    )
    _install(monkeypatch, loader)
    assert set(_auto_loaded_dependencies("sensor", "leaf")) == {"dep_a", "dep_b"}


def test_auto_loaded_names_are_not_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auto-loaded components are always present; only their requirements propagate."""
    loader = _FakeLoader(
        components={"base": _manifest()},
        platforms={("sensor", "leaf"): _manifest(auto_load=["base"])},
    )
    _install(monkeypatch, loader)
    assert _auto_loaded_dependencies("sensor", "leaf") == ()


def test_dep_on_a_sibling_auto_load_is_subtracted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A closure member's dep that another member auto-loads is already present."""
    loader = _FakeLoader(
        components={
            "graph": _manifest(dependencies=["display", "sensor"]),
            "sensor": _manifest(),
        },
        platforms={("display", "leaf"): _manifest(auto_load=["graph", "sensor"])},
    )
    _install(monkeypatch, loader)
    assert _auto_loaded_dependencies("display", "leaf") == ("display",)


def test_bare_component_resolves_without_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    loader = _FakeLoader(
        components={
            "hub": _manifest(auto_load=["base"]),
            "base": _manifest(dependencies=["uart"]),
        },
        platforms={},
    )
    _install(monkeypatch, loader)
    assert _auto_loaded_dependencies("", "hub") == ("uart",)


def test_missing_manifest_or_loader_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _FakeLoader(components={}, platforms={}))
    assert _auto_loaded_dependencies("climate", "nope") == ()
    _auto_loaded_dependencies.cache_clear()
    monkeypatch.setattr(sc, "_get_esphome_loader", lambda: None)
    assert _auto_loaded_dependencies("climate", "climate_ir_lg") == ()


def test_real_esphome_climate_ir_lg_gains_remote_transmitter() -> None:
    """The #1991 shape against the installed esphome."""
    pytest.importorskip("esphome")
    assert "remote_transmitter" in _auto_loaded_dependencies("climate", "climate_ir_lg")
