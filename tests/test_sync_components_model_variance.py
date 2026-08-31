"""Tests for the sync's per-model requiredness introspection.

Synthetic schemas pin the collector/applier; the integration tests
run against the live ``epaper_spi`` / ``mipi_*`` manifests to catch
upstream reshapes of the extractor closure.
"""

from __future__ import annotations

from collections.abc import Iterator

import esphome.config_validation as cv
import pytest
import voluptuous as vol
from esphome.components.mipi import model_schema_extractor

from script.sync_components import (  # type: ignore[import-not-found]
    _UNHANDLED_MODEL_DRIVEN,
    ModelField,
    ModelVariance,
    _apply_model_variance,
    _collect_model_variance,
    _fail_on_unhandled_model_driven,
    _get_esphome_loader,
)


@pytest.fixture(autouse=True)
def _clean_model_driven_canary() -> Iterator[None]:
    """Clear the module-level canary accumulator around every test."""
    _UNHANDLED_MODEL_DRIVEN.clear()
    yield
    _UNHANDLED_MODEL_DRIVEN.clear()


class _FakeManifest:
    """Minimal manifest stub — only ``config_schema`` is read."""

    def __init__(self, schema: object) -> None:
        self.config_schema = schema


def _extracted_schema():
    """Build a two-model CONFIG_SCHEMA decorated by the real mipi extractor."""
    models = {"A": None, "B": None}

    def model_schema(config: dict) -> cv.Schema:
        if config["model"] == "A":
            return cv.Schema(
                {
                    cv.Required("dc_pin"): cv.string,
                    cv.Optional("width", default=10): cv.int_,
                    cv.GenerateID(): cv.declare_id(int),
                }
            )
        return cv.Schema(
            {
                cv.Optional("dc_pin", default=5): cv.string,
                cv.Optional("width", default=10): cv.int_,
                cv.Optional("b_only"): cv.string,
            }
        )

    @model_schema_extractor(models, model_schema)
    def config_schema(config):
        return model_schema(config)(config)

    return config_schema


# ---------------------------------------------------------------------------
# _collect_model_variance
# ---------------------------------------------------------------------------


def test_collect_reads_per_model_facts() -> None:
    variance = _collect_model_variance(_FakeManifest(_extracted_schema()), "display.fake")
    assert variance is not None
    assert variance.models == ("A", "B")
    dc = variance.fields["dc_pin"]
    assert dc["A"].required is True
    assert dc["A"].default is vol.UNDEFINED
    assert dc["B"].required is False
    assert dc["B"].default == 5
    width = variance.fields["width"]
    assert {m: f.required for m, f in width.items()} == {"A": False, "B": False}
    assert {m: f.default for m, f in width.items()} == {"A": 10, "B": 10}
    assert set(variance.fields["b_only"]) == {"B"}
    assert not any(key.startswith("id") for key in variance.fields)


def test_collect_passes_extra_through() -> None:
    seen: list[dict] = []
    models = {"A": None}

    def model_schema(config: dict) -> cv.Schema:
        seen.append(dict(config))
        return cv.Schema({})

    @model_schema_extractor(models, model_schema, extra={"bus_mode": "single"})
    def config_schema(config):
        return config

    variance = _collect_model_variance(_FakeManifest(config_schema), "display.fake")
    assert variance is not None
    assert seen == [{"model": "A", "bus_mode": "single"}]


def test_collect_ignores_plain_schemas() -> None:
    def plain(config):
        return config

    assert _collect_model_variance(_FakeManifest(plain), "x") is None
    assert _collect_model_variance(_FakeManifest(cv.Schema({})), "x") is None
    assert _collect_model_variance(_FakeManifest(None), "x") is None
    assert not _UNHANDLED_MODEL_DRIVEN


def test_unknown_model_driven_closure_fails_loudly() -> None:
    models = {"A": None}

    def model_schema(config):
        return cv.Schema({})

    def impostor(config):
        return model_schema({**config, "models": models})

    assert _collect_model_variance(_FakeManifest(impostor), "display.impostor") is None
    assert "display.impostor" in _UNHANDLED_MODEL_DRIVEN
    with pytest.raises(SystemExit, match=r"display\.impostor"):
        _fail_on_unhandled_model_driven()


# ---------------------------------------------------------------------------
# _apply_model_variance
# ---------------------------------------------------------------------------


def _entry(key: str, *, required: bool = False, advanced: bool = False, **extra) -> dict:
    return {
        "key": key,
        "required": required,
        "advanced": advanced,
        "default_value": None,
        "depends_on": None,
        **extra,
    }


def _model_entry(*values: str) -> dict:
    return _entry("model", required=True, options=[{"label": v, "value": v} for v in values])


def test_mixed_field_splits_into_gated_twins() -> None:
    entries = [
        _model_entry("A", "B", "C"),
        _entry(
            "dc_pin",
            required=True,
            config_entries=[{"key": "number"}],
        ),
    ]
    variance = ModelVariance(
        ("A", "B", "C"),
        {
            "dc_pin": {
                "A": ModelField(required=True, default=vol.UNDEFINED),
                "B": ModelField(required=False, default=5),
                "C": ModelField(required=False, default=vol.UNDEFINED),
            }
        },
    )
    _apply_model_variance(entries, variance, "display.fake")
    twins = [e for e in entries if e["key"] == "dc_pin"]
    assert len(twins) == 2
    required_twin, optional_twin = twins
    assert required_twin["required"] is True
    assert required_twin["advanced"] is False
    assert required_twin["depends_on"] == "model"
    assert required_twin["depends_on_value_any"] == ["A"]
    assert optional_twin["required"] is False
    assert optional_twin["depends_on_value_any"] == ["B", "C"]
    # Varying defaults are scrubbed on the optional twin too.
    assert optional_twin["default_value"] is None
    # Deep copies: mutating one twin's subtree must not leak into the other.
    required_twin["config_entries"][0]["key"] = "mutated"
    assert optional_twin["config_entries"][0]["key"] == "number"


def test_never_required_field_is_demoted() -> None:
    entries = [_model_entry("A", "B"), _entry("cs_pin", required=True)]
    variance = ModelVariance(
        ("A", "B"),
        {
            "cs_pin": {
                "A": ModelField(required=False, default=vol.UNDEFINED),
                "B": ModelField(required=False, default=vol.UNDEFINED),
            }
        },
    )
    _apply_model_variance(entries, variance, "display.fake")
    (cs,) = [e for e in entries if e["key"] == "cs_pin"]
    assert cs["required"] is False
    assert cs["depends_on"] is None


def test_uniform_default_kept_varying_default_scrubbed() -> None:
    entries = [
        _model_entry("A", "B"),
        _entry("data_rate", default_value="20MHz"),
        _entry("update_interval", default_value="60s"),
    ]
    variance = ModelVariance(
        ("A", "B"),
        {
            "data_rate": {
                "A": ModelField(required=False, default=20_000_000),
                "B": ModelField(required=False, default=10_000_000),
            },
            "update_interval": {
                "A": ModelField(required=False, default="60s"),
                "B": ModelField(required=False, default="60s"),
            },
        },
    )
    _apply_model_variance(entries, variance, "display.fake")
    by_key = {e["key"]: e for e in entries}
    assert by_key["data_rate"]["default_value"] is None
    assert by_key["update_interval"]["default_value"] == "60s"


def test_field_absent_from_some_models_is_gated_to_carriers() -> None:
    entries = [_model_entry("A", "B", "C"), _entry("init_sequence")]
    variance = ModelVariance(
        ("A", "B", "C"),
        {
            "init_sequence": {
                "B": ModelField(required=False, default=vol.UNDEFINED),
            }
        },
    )
    _apply_model_variance(entries, variance, "display.fake")
    (init,) = [e for e in entries if e["key"] == "init_sequence"]
    assert init["depends_on"] == "model"
    assert init["depends_on_value_any"] == ["B"]
    assert init["required"] is False


def test_field_without_catalog_entry_is_skipped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    entries = [_model_entry("A")]
    variance = ModelVariance(
        ("A",),
        {
            "cs1_pin": {"A": ModelField(required=True, default=vol.UNDEFINED)},
            "brightness": {"A": ModelField(required=False, default=vol.UNDEFINED)},
        },
    )
    with caplog.at_level("INFO", logger="sync_components"):
        _apply_model_variance(entries, variance, "display.fake")
    assert [e["key"] for e in entries] == ["model"]
    by_message = {r.message: r.levelname for r in caplog.records if "no catalog entry" in r.message}
    assert any("cs1_pin" in m and level == "WARNING" for m, level in by_message.items())
    assert any("brightness" in m and level == "INFO" for m, level in by_message.items())


def test_pre_gated_field_fails_loudly() -> None:
    entries = [_model_entry("A"), _entry("dc_pin", required=True, depends_on="variant")]
    variance = ModelVariance(
        ("A",), {"dc_pin": {"A": ModelField(required=False, default=vol.UNDEFINED)}}
    )
    with pytest.raises(SystemExit, match="already gated"):
        _apply_model_variance(entries, variance, "display.fake")


def test_unresolvable_model_schema_fails_loudly() -> None:
    models = {"A": None}

    def model_schema(config: dict):
        return object()

    @model_schema_extractor(models, model_schema)
    def config_schema(config):
        return config

    with pytest.raises(SystemExit, match=r"model 'A'"):
        _collect_model_variance(_FakeManifest(config_schema), "display.fake")


def test_literal_marker_default_is_recorded() -> None:
    models = {"A": None}
    schema = cv.Schema({cv.Optional("x"): cv.string})
    marker = next(iter(schema.schema))
    marker.default = 7

    @model_schema_extractor(models, lambda config: schema)
    def config_schema(config):
        return config

    variance = _collect_model_variance(_FakeManifest(config_schema), "display.fake")
    assert variance is not None
    assert variance.fields["x"]["A"].default == 7


def test_model_enum_mismatch_fails_loudly() -> None:
    entries = [_model_entry("A"), _entry("dc_pin", required=True)]
    variance = ModelVariance(
        ("A", "B"), {"dc_pin": {"A": ModelField(required=True, default=vol.UNDEFINED)}}
    )
    with pytest.raises(SystemExit, match=r"display\.fake"):
        _apply_model_variance(entries, variance, "display.fake")


# ---------------------------------------------------------------------------
# Live manifests
# ---------------------------------------------------------------------------


def test_live_epaper_spi_variance() -> None:
    loader = _get_esphome_loader()
    assert loader is not None
    manifest = loader.get_platform("display", "epaper_spi")
    variance = _collect_model_variance(manifest, "display.epaper_spi")
    assert variance is not None
    cs = variance.fields["cs_pin"]
    assert not any(f.required for f in cs.values())
    dc_required = {f.required for f in variance.fields["dc_pin"].values()}
    dimensions_required = {f.required for f in variance.fields["dimensions"].values()}
    assert dc_required == {True, False}
    assert dimensions_required == {True, False}
    assert not _UNHANDLED_MODEL_DRIVEN


def test_live_detection_scope() -> None:
    loader = _get_esphome_loader()
    assert loader is not None
    for platform in ("epaper_spi", "mipi_spi", "mipi_dsi", "mipi_rgb"):
        manifest = loader.get_platform("display", platform)
        assert _collect_model_variance(manifest, f"display.{platform}") is not None
    ssd1306 = loader.get_platform("display", "ssd1306_spi")
    assert _collect_model_variance(ssd1306, "display.ssd1306_spi") is None
    assert not _UNHANDLED_MODEL_DRIVEN
