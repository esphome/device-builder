"""Unit tests for the light schema derivation helpers (``script/_light_schemas.py``).

Fixture-based against a synthetic ``components/`` tree under ``tmp_path``
so the resolver contract is pinned independent of the live upstream
esphome layout — the indirect chase through ``fastled_base`` is the
main failure surface, plus the OSError / JSONDecodeError branches.
"""

from __future__ import annotations

import builtins
import json
import logging
from pathlib import Path

import pytest

from script._light_schemas import (
    derive_light_platforms_by_schema,
    derive_light_platforms_from_dir,
    resolve_light_effects_applies_to,
    resolve_schema_ref,
)


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def test_resolve_schema_ref_direct(tmp_path: Path) -> None:
    components = tmp_path / "components"
    _write(
        components / "neopixelbus" / "light.py",
        "BASE = light.ADDRESSABLE_LIGHT_SCHEMA.extend({...})\n",
    )
    ref = resolve_schema_ref(
        components / "neopixelbus" / "light.py",
        components,
        set(),
    )
    assert ref == "ADDRESSABLE_LIGHT_SCHEMA"


def test_resolve_schema_ref_follows_transitive_helper(tmp_path: Path) -> None:
    components = tmp_path / "components"
    _write(
        components / "fastled_base" / "__init__.py",
        "BASE_SCHEMA = light.ADDRESSABLE_LIGHT_SCHEMA.extend({...})\n",
    )
    _write(
        components / "fastled_clockless" / "light.py",
        "CONFIG_SCHEMA = fastled_base.BASE_SCHEMA.extend({...})\n",
    )
    ref = resolve_schema_ref(
        components / "fastled_clockless" / "light.py",
        components,
        set(),
    )
    assert ref == "ADDRESSABLE_LIGHT_SCHEMA"


def test_resolve_schema_ref_skips_non_component_modules(tmp_path: Path) -> None:
    components = tmp_path / "components"
    _write(
        components / "lonely" / "light.py",
        # cv.Schema mentions ``cv.Schema``; the resolver must not chase
        # out of ``components/`` (no ``components/cv/`` exists, so the
        # candidates are empty and the recursion bails).
        "CONFIG_SCHEMA = cv.Schema({...})\n",
    )
    ref = resolve_schema_ref(
        components / "lonely" / "light.py",
        components,
        set(),
    )
    assert ref is None


def test_resolve_schema_ref_missing_file_returns_none(tmp_path: Path) -> None:
    components = tmp_path / "components"
    components.mkdir()
    ref = resolve_schema_ref(
        components / "nope" / "light.py",
        components,
        set(),
    )
    assert ref is None


def test_resolve_schema_ref_oserror_is_logged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Monkeypatch ``Path.read_text`` instead of ``chmod(0o000)`` so the
    # OSError branch is exercised consistently across Linux / macOS /
    # Windows (Windows ACLs ignore POSIX permission bits).
    components = tmp_path / "components"
    target = components / "broken" / "light.py"
    _write(target, "BASE = light.ADDRESSABLE_LIGHT_SCHEMA\n")

    original = Path.read_text

    def boom(self: Path, *args: object, **kwargs: object) -> str:
        if self == target:
            raise PermissionError("forced for test")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", boom)
    with caplog.at_level(logging.WARNING, logger="script._light_schemas"):
        ref = resolve_schema_ref(target, components, set())
    assert ref is None
    assert any("failed to read" in r.message for r in caplog.records)


def test_derive_light_platforms_from_dir_buckets_by_schema(tmp_path: Path) -> None:
    components = tmp_path / "components"
    _write(
        components / "neopixelbus" / "light.py",
        "BASE = light.ADDRESSABLE_LIGHT_SCHEMA\n",
    )
    _write(
        components / "rgb" / "light.py",
        "BASE = light.RGB_LIGHT_SCHEMA\n",
    )
    _write(
        components / "monochromatic" / "light.py",
        "BASE = light.BRIGHTNESS_ONLY_LIGHT_SCHEMA\n",
    )
    _write(
        components / "binary" / "light" / "__init__.py",
        "BASE = light.BINARY_LIGHT_SCHEMA\n",
    )
    out = derive_light_platforms_from_dir(components)
    assert out["ADDRESSABLE_LIGHT_SCHEMA"] == frozenset({"light.neopixelbus"})
    assert out["RGB_LIGHT_SCHEMA"] == frozenset({"light.rgb"})
    assert out["BRIGHTNESS_ONLY_LIGHT_SCHEMA"] == frozenset({"light.monochromatic"})
    assert out["BINARY_LIGHT_SCHEMA"] == frozenset({"light.binary"})


def test_derive_light_platforms_from_dir_picks_up_fastled_transitively(
    tmp_path: Path,
) -> None:
    components = tmp_path / "components"
    _write(
        components / "fastled_base" / "__init__.py",
        "BASE_SCHEMA = light.ADDRESSABLE_LIGHT_SCHEMA.extend({...})\n",
    )
    _write(
        components / "fastled_clockless" / "light.py",
        "CONFIG_SCHEMA = fastled_base.BASE_SCHEMA.extend({...})\n",
    )
    _write(
        components / "fastled_spi" / "light.py",
        "CONFIG_SCHEMA = fastled_base.BASE_SCHEMA.extend({...})\n",
    )
    out = derive_light_platforms_from_dir(components)
    assert out["ADDRESSABLE_LIGHT_SCHEMA"] == frozenset(
        {"light.fastled_clockless", "light.fastled_spi"}
    )


def test_derive_light_platforms_by_schema_falls_back_to_empty_when_esphome_missing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The cache survives across tests; clear it so this run sees the
    # forced ImportError instead of a cached real-esphome result.
    derive_light_platforms_by_schema.cache_clear()
    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "esphome":
            raise ImportError("forced for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with caplog.at_level(logging.WARNING, logger="script._light_schemas"):
        out = derive_light_platforms_by_schema()
    assert out == {}
    assert any("esphome not importable" in r.message for r in caplog.records)
    derive_light_platforms_by_schema.cache_clear()


def _make_light_json(
    schema_dir: Path,
    *,
    addressable_effects: list[str] | None = None,
    rgb_effects: list[str] | None = None,
) -> None:
    """Write a minimal upstream-style ``light.json`` schema bundle."""
    body = {
        "light": {
            "schemas": {
                "ADDRESSABLE_LIGHT_SCHEMA": {
                    "schema": {
                        "config_vars": {
                            "effects": {"filter": addressable_effects or []},
                        },
                    },
                },
                "RGB_LIGHT_SCHEMA": {
                    "schema": {
                        "config_vars": {
                            "effects": {"filter": rgb_effects or []},
                        },
                    },
                },
            },
        },
    }
    (schema_dir / "light.json").write_text(json.dumps(body))


def test_resolve_light_effects_applies_to_uses_per_schema_filter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    derive_light_platforms_by_schema.cache_clear()
    # Pin the derivation: addressable_rainbow only on
    # ADDRESSABLE_LIGHT_SCHEMA, which maps to one platform.
    monkeypatch.setattr(
        "script._light_schemas.derive_light_platforms_by_schema",
        lambda: {
            "ADDRESSABLE_LIGHT_SCHEMA": frozenset({"light.esp32_rmt_led_strip"}),
            "RGB_LIGHT_SCHEMA": frozenset({"light.rgb"}),
            "BRIGHTNESS_ONLY_LIGHT_SCHEMA": frozenset(),
            "BINARY_LIGHT_SCHEMA": frozenset(),
        },
    )
    _make_light_json(
        tmp_path,
        addressable_effects=["addressable_rainbow"],
        rgb_effects=["pulse", "strobe"],
    )
    assert resolve_light_effects_applies_to("addressable_rainbow", tmp_path) == [
        "light.esp32_rmt_led_strip"
    ]
    assert resolve_light_effects_applies_to("pulse", tmp_path) == ["light.rgb"]
    assert resolve_light_effects_applies_to("unknown_effect", tmp_path) == []
    derive_light_platforms_by_schema.cache_clear()


def test_resolve_light_effects_applies_to_missing_schema_returns_empty(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # No light.json under tmp_path → empty applies_to + a logged warning
    # (distinct from "corrupt schema").
    with caplog.at_level(logging.WARNING, logger="script._light_schemas"):
        out = resolve_light_effects_applies_to("rainbow", tmp_path)
    assert out == []
    assert any("Light schema missing" in r.message for r in caplog.records)


def test_resolve_light_effects_applies_to_corrupt_schema_raises(
    tmp_path: Path,
) -> None:
    # A partially-written / corrupt light.json indicates a real upstream
    # bug; the resolver lets JSONDecodeError propagate rather than
    # silently shipping an empty applies_to for every effect.
    (tmp_path / "light.json").write_text("{ not valid json")
    with pytest.raises(json.JSONDecodeError):
        resolve_light_effects_applies_to("rainbow", tmp_path)
