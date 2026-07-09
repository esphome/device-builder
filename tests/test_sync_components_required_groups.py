"""Tests for schema-derived cross-field constraints in the sync script.

Upstream esphome's ``cv.has_*_one_key`` and ``cv.Inclusive``
validators express "must specify one of these" / "all-or-nothing"
rules across sibling keys, but the pre-built JSON schema bundle
flattens both away — every key shows up as plain ``Optional``.
Without the introspection in this module, the catalog has no way
to know that ``light.esp32_rmt_led_strip`` needs *either* a
``chipset`` *or* the manual-timing group (issue #924), and the
form happily hides both behind the Advanced toggle.

Most tests pin the walker against synthetic voluptuous schemas
— synthetic keeps the suite stable across upstream refactors. Two
integration tests run against the live ``esp32_rmt_led_strip.light``
and ``wifi`` manifests to catch regressions where the algorithm
is right against synthetic schemas but breaks against real
upstream shapes (a new combinator class, a custom wrapper around
``cv.Inclusive``, etc.).
"""

from __future__ import annotations

import sys
import types

import esphome.config_validation as cv
import pytest
import voluptuous as vol

from script.sync_components import (  # type: ignore[import-not-found]
    _annotate_constraint_descriptions,
    _apply_inclusive_groups,
    _apply_required_groups,
    _collect_automation_registry_groups,
    _collect_inclusive_groups,
    _collect_required_groups,
    _groups_in_all_chain,
    _promote_constraint_members,
    _required_group_from_validator,
)


class _FakeManifest:
    """Minimal manifest stub — only ``config_schema`` is read."""

    def __init__(self, schema: object) -> None:
        self.config_schema = schema


# ---------------------------------------------------------------------------
# _required_group_from_validator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("factory", "expected_kind"),
    [
        (cv.has_exactly_one_key, "exactly_one"),
        (cv.has_at_least_one_key, "at_least_one"),
        (cv.has_at_most_one_key, "at_most_one"),
        (cv.has_none_or_all_keys, "none_or_all"),
    ],
)
def test_required_group_from_validator_recovers_each_kind(
    factory: object,
    expected_kind: str,
) -> None:
    """Every ``cv.has_*_one_key`` flavour resolves to its wire kind."""
    validator = factory("foo", "bar")  # type: ignore[operator]
    assert _required_group_from_validator(validator) == {
        "kind": expected_kind,
        "keys": ["foo", "bar"],
    }


def test_required_group_from_validator_returns_none_for_unrelated_validator() -> None:
    """A validator outside the ``has_*_one_key`` family yields ``None``."""
    assert _required_group_from_validator(cv.string) is None
    assert _required_group_from_validator(cv.boolean) is None
    assert _required_group_from_validator(lambda x: x) is None


def test_required_group_from_validator_drops_non_string_keys() -> None:
    """Non-string captured keys are filtered out so the wire shape stays clean.

    Voluptuous accepts any hashable key, so a schema author *could*
    pass non-string keys to ``cv.has_at_least_one_key``; we'd
    rather surface only the string ones than an unserialisable
    mixed-type list.
    """
    validator = cv.has_exactly_one_key(123, None, "real_key")  # type: ignore[arg-type]
    assert _required_group_from_validator(validator) == {
        "kind": "exactly_one",
        "keys": ["real_key"],
    }


# ---------------------------------------------------------------------------
# _collect_inclusive_groups
# ---------------------------------------------------------------------------


def test_collect_inclusive_groups_returns_empty_for_unconstrained_schema() -> None:
    """A schema with no ``cv.Inclusive`` markers returns an empty dict."""
    schema = {
        cv.Optional("free"): cv.string,
        cv.Optional("loop_time"): cv.string,
    }
    assert _collect_inclusive_groups(_FakeManifest(schema)) == {}


def test_collect_inclusive_groups_records_top_level_marker() -> None:
    """A top-level ``cv.Inclusive`` surfaces at its single-element path."""
    schema = {
        cv.Optional("plain"): cv.string,
        cv.Inclusive("foo", "pair"): cv.string,
        cv.Inclusive("bar", "pair"): cv.string,
    }
    out = _collect_inclusive_groups(_FakeManifest(schema))
    assert out == {("foo",): "pair", ("bar",): "pair"}


def test_collect_inclusive_groups_walks_nested_vol_all_values() -> None:
    """Inclusive markers inside a nested ``vol.All`` are still reached.

    Mirrors the upstream ``wifi.eap`` shape:
    ``EAP_AUTH_SCHEMA = cv.All(cv.only_on(...), cv.Schema({Inclusive(...)}), ...)``.
    The walker has to descend past the outer ``vol.All`` and the
    inner ``cv.Schema`` to see the Inclusive keys.
    """
    eap_inner = cv.Schema(
        {
            cv.Optional("identity"): cv.string,
            cv.Inclusive("certificate", "cert_and_key"): cv.string,
            cv.Inclusive("key", "cert_and_key"): cv.string,
        },
    )
    eap = vol.All(cv.only_on_esp32, eap_inner, lambda x: x)
    schema = cv.Schema({cv.Optional("eap"): eap})
    out = _collect_inclusive_groups(_FakeManifest(schema))
    assert out == {
        ("eap", "certificate"): "cert_and_key",
        ("eap", "key"): "cert_and_key",
    }


def test_collect_inclusive_groups_returns_empty_when_manifest_has_no_schema() -> None:
    """Missing ``config_schema`` is handled gracefully."""

    class NoSchemaManifest:
        config_schema = None

    assert _collect_inclusive_groups(NoSchemaManifest()) == {}


# ---------------------------------------------------------------------------
# _collect_required_groups
# ---------------------------------------------------------------------------


def test_collect_required_groups_returns_empty_for_unconstrained_schema() -> None:
    """A schema with no ``has_*_one_key`` wrapper returns an empty dict."""
    schema = cv.Schema({cv.Optional("plain"): cv.string})
    assert _collect_required_groups(_FakeManifest(schema)) == {}


def test_collect_required_groups_captures_top_level_constraint() -> None:
    """A constraint wrapping the top-level schema lands at path ``()``.

    Same shape upstream uses for ``light.esp32_rmt_led_strip``:
    ``cv.All(cv.Schema({...}), cv.has_exactly_one_key("a", "b"))``.
    """
    schema = cv.All(
        cv.Schema(
            {
                cv.Optional("chipset"): cv.string,
                cv.Optional("bit0_high"): cv.positive_time_period_nanoseconds,
            },
        ),
        cv.has_exactly_one_key("chipset", "bit0_high"),
    )
    out = _collect_required_groups(_FakeManifest(schema))
    assert out == {
        (): [{"kind": "exactly_one", "keys": ["chipset", "bit0_high"]}],
    }


def test_collect_required_groups_captures_nested_constraint() -> None:
    """A constraint on a nested sub-schema lands at the nested path.

    Mirrors ``wifi.networks[].eap`` (and the simpler ``wifi.eap``):
    the cv.All wrapping the inner schema carries
    ``cv.has_at_least_one_key(...)`` alongside the dict.
    """
    eap_inner = cv.Schema(
        {
            cv.Optional("identity"): cv.string,
            cv.Optional("certificate"): cv.string,
        },
    )
    eap = vol.All(eap_inner, cv.has_at_least_one_key("identity", "certificate"))
    schema = cv.Schema({cv.Optional("eap"): eap})
    out = _collect_required_groups(_FakeManifest(schema))
    assert out == {
        ("eap",): [{"kind": "at_least_one", "keys": ["identity", "certificate"]}],
    }


def test_collect_required_groups_handles_all_four_kinds() -> None:
    """Every ``cv.has_*_one_key`` flavour serialises to its wire kind."""
    schema = cv.All(
        cv.Schema({cv.Optional("a"): cv.string, cv.Optional("b"): cv.string}),
        cv.has_exactly_one_key("a", "b"),
        cv.has_at_least_one_key("a", "b"),
        cv.has_at_most_one_key("a", "b"),
        cv.has_none_or_all_keys("a", "b"),
    )
    out = _collect_required_groups(_FakeManifest(schema))
    assert out == {
        (): [
            {"kind": "exactly_one", "keys": ["a", "b"]},
            {"kind": "at_least_one", "keys": ["a", "b"]},
            {"kind": "at_most_one", "keys": ["a", "b"]},
            {"kind": "none_or_all", "keys": ["a", "b"]},
        ],
    }


def test_collect_required_groups_returns_empty_when_manifest_has_no_schema() -> None:
    """Missing ``config_schema`` is handled gracefully."""

    class NoSchemaManifest:
        config_schema = None

    assert _collect_required_groups(NoSchemaManifest()) == {}


# ---------------------------------------------------------------------------
# Appliers
# ---------------------------------------------------------------------------


def test_apply_inclusive_groups_stamps_group_on_matching_entries() -> None:
    """The applier walks catalog entries and copies the group name in."""
    entries = [
        {"key": "chipset", "config_entries": []},
        {"key": "bit0_high", "config_entries": []},
        {"key": "bit0_low", "config_entries": []},
    ]
    groups = {("bit0_high",): "custom", ("bit0_low",): "custom"}
    _apply_inclusive_groups(entries, groups)
    by_key = {e["key"]: e for e in entries}
    assert by_key["bit0_high"]["group"] == "custom"
    assert by_key["bit0_low"]["group"] == "custom"
    assert "group" not in by_key["chipset"]


def test_apply_inclusive_groups_is_a_no_op_when_empty() -> None:
    """Empty group dict leaves entries untouched."""
    entries = [{"key": "ssid", "config_entries": []}]
    before = [dict(e) for e in entries]
    _apply_inclusive_groups(entries, {})
    assert entries == before


def test_apply_required_groups_stamps_component_root() -> None:
    """``path=()`` constraints land on the component dict itself."""
    component = {
        "id": "light.esp32_rmt_led_strip",
        "config_entries": [
            {"key": "chipset", "config_entries": []},
            {"key": "bit0_high", "config_entries": []},
        ],
    }
    groups = {(): [{"kind": "exactly_one", "keys": ["chipset", "bit0_high"]}]}
    _apply_required_groups(component, groups)
    assert component["required_groups"] == [
        {"kind": "exactly_one", "keys": ["chipset", "bit0_high"]},
    ]


def test_apply_required_groups_stamps_nested_entry() -> None:
    """Non-empty paths target the matching nested ``NESTED`` entry."""
    component = {
        "id": "wifi",
        "config_entries": [
            {
                "key": "eap",
                "config_entries": [
                    {"key": "identity", "config_entries": []},
                    {"key": "certificate", "config_entries": []},
                ],
            },
        ],
    }
    groups = {
        ("eap",): [{"kind": "at_least_one", "keys": ["identity", "certificate"]}],
    }
    _apply_required_groups(component, groups)
    assert component.get("required_groups", []) == []
    assert component["config_entries"][0]["required_groups"] == [
        {"kind": "at_least_one", "keys": ["identity", "certificate"]},
    ]


def test_apply_required_groups_is_a_no_op_when_empty() -> None:
    """Empty constraints dict leaves the component untouched."""
    component = {"id": "x", "config_entries": [{"key": "foo", "config_entries": []}]}
    before = {"id": component["id"], "config_entries": [dict(component["config_entries"][0])]}
    _apply_required_groups(component, {})
    assert "required_groups" not in component
    assert component["config_entries"] == before["config_entries"]


def test_apply_required_groups_drops_paths_with_no_catalog_match() -> None:
    """Constraints whose path doesn't match any entry are silently ignored.

    The schema walker can produce paths for synthetic constructs
    (``cv.ensure_list`` wrappers, internal markers) that don't have
    a catalog counterpart. The applier must not crash on those.
    """
    component = {"id": "x", "config_entries": [{"key": "real", "config_entries": []}]}
    groups = {("ghost", "missing"): [{"kind": "exactly_one", "keys": ["a", "b"]}]}
    _apply_required_groups(component, groups)
    assert "required_groups" not in component["config_entries"][0]


# ---------------------------------------------------------------------------
# _promote_constraint_members
# ---------------------------------------------------------------------------


def test_promote_constraint_members_demotes_referenced_keys() -> None:
    """Fields named in a constraint get pulled off ``advanced``."""
    entries = [
        {"key": "chipset", "advanced": True},
        {"key": "rgb_order", "advanced": False},
        {"key": "bit0_high", "advanced": True, "group": "custom"},
    ]
    groups = [{"kind": "exactly_one", "keys": ["chipset", "bit0_high"]}]
    out = _promote_constraint_members(entries, groups)
    by_key = {e["key"]: e for e in out}
    assert by_key["chipset"]["advanced"] is False
    assert by_key["bit0_high"]["advanced"] is False
    # Untouched sibling stays at whatever the schema author picked.
    assert by_key["rgb_order"]["advanced"] is False


def test_promote_constraint_members_also_pulls_inclusive_partners() -> None:
    """An Inclusive sibling sharing a referenced field's group also promotes.

    Upstream ``has_exactly_one_key`` typically names one
    representative key per branch (e.g. ``"bit0_high"`` stands in
    for the whole timing group). Promoting only that
    representative would leave the user staring at the timing
    fields' partners still under Advanced — the whole point of the
    Inclusive group is they belong together.
    """
    entries = [
        {"key": "chipset", "advanced": True},
        {"key": "bit0_high", "advanced": True, "group": "custom"},
        {"key": "bit0_low", "advanced": True, "group": "custom"},
        {"key": "bit1_high", "advanced": True, "group": "custom"},
        {"key": "bit1_low", "advanced": True, "group": "custom"},
        {"key": "is_rgbw", "advanced": True},  # unrelated, stays advanced
    ]
    groups = [{"kind": "exactly_one", "keys": ["chipset", "bit0_high"]}]
    out = _promote_constraint_members(entries, groups)
    by_key = {e["key"]: e for e in out}
    for key in ("chipset", "bit0_high", "bit0_low", "bit1_high", "bit1_low"):
        assert by_key[key]["advanced"] is False, f"{key} should be promoted"
    assert by_key["is_rgbw"]["advanced"] is True


def test_promote_constraint_members_returns_same_list_on_no_op() -> None:
    """When nothing was advanced, the original list is returned unchanged.

    Identity check matters: the catch-all re-sort would otherwise
    perturb a list the schema authors deliberately ordered.
    """
    entries = [
        {"key": "chipset", "advanced": False},
        {"key": "bit0_high", "advanced": False},
    ]
    groups = [{"kind": "exactly_one", "keys": ["chipset", "bit0_high"]}]
    out = _promote_constraint_members(entries, groups)
    assert out is entries


def test_promote_constraint_members_resorts_after_demoting() -> None:
    """Re-sorting moves the demoted entries ahead of remaining advanced siblings."""
    entries = [
        {"key": "advanced_first", "advanced": False},
        {"key": "later_advanced", "advanced": True},
        {"key": "promoted", "advanced": True},
    ]
    groups = [{"kind": "exactly_one", "keys": ["promoted"]}]
    out = _promote_constraint_members(entries, groups)
    # Non-advanced entries lead; previously-advanced siblings trail.
    assert [e["key"] for e in out[:2]] == ["advanced_first", "promoted"]
    assert out[-1]["key"] == "later_advanced"


# ---------------------------------------------------------------------------
# _annotate_constraint_descriptions
# ---------------------------------------------------------------------------


def test_annotate_descriptions_prepends_exactly_one_hint() -> None:
    """A field referenced in ``exactly_one`` gets the prose hint above its docs."""
    component = {
        "config_entries": [
            {"key": "chipset", "description": "Pick a chipset."},
            {"key": "bit0_high", "description": "Bit 0 high time."},
            {"key": "rgb_order", "description": "RGB ordering."},
        ],
        "required_groups": [{"kind": "exactly_one", "keys": ["chipset", "bit0_high"]}],
    }
    _annotate_constraint_descriptions(component)
    by_key = {e["key"]: e for e in component["config_entries"]}
    assert by_key["chipset"]["description"].startswith(
        "**Required — set exactly one of:** `chipset`, `bit0_high`.",
    )
    assert "Pick a chipset." in by_key["chipset"]["description"]
    assert by_key["bit0_high"]["description"].startswith(
        "**Required — set exactly one of:** `chipset`, `bit0_high`.",
    )
    # Unrelated sibling stays untouched.
    assert by_key["rgb_order"]["description"] == "RGB ordering."


def test_annotate_descriptions_handles_each_kind() -> None:
    """Each ``cv.has_*_one_key`` flavour gets its own readable prefix."""
    component = {
        "config_entries": [
            {"key": "a", "description": ""},
            {"key": "b", "description": ""},
        ],
        "required_groups": [
            {"kind": "at_least_one", "keys": ["a", "b"]},
            {"kind": "at_most_one", "keys": ["a", "b"]},
            {"kind": "none_or_all", "keys": ["a", "b"]},
        ],
    }
    _annotate_constraint_descriptions(component)
    desc_a = component["config_entries"][0]["description"]
    assert "**Required — set at least one of:** `a`, `b`." in desc_a
    assert "**Set at most one of:** `a`, `b`." in desc_a
    assert "**Set together — all of these must be set, or all left blank:**" in desc_a


def test_annotate_descriptions_appends_inclusive_group_hint() -> None:
    """``cv.Inclusive`` siblings get a "set together with" hint listing partners."""
    component = {
        "config_entries": [
            {"key": "bit0_high", "description": "0-bit high.", "group": "custom"},
            {"key": "bit0_low", "description": "0-bit low.", "group": "custom"},
            {"key": "bit1_high", "description": "1-bit high.", "group": "custom"},
            {"key": "bit1_low", "description": "1-bit low.", "group": "custom"},
        ],
        "required_groups": [],
    }
    _annotate_constraint_descriptions(component)
    desc = component["config_entries"][0]["description"]
    # ``bit0_high`` itself isn't listed; the other three are.
    assert "`bit0_low`" in desc and "`bit1_high`" in desc and "`bit1_low`" in desc
    assert "`bit0_high`" not in desc.split("\n\n")[0]  # not in the prefix
    assert "(all-or-none)" in desc
    assert "0-bit high." in desc


def test_annotate_descriptions_composes_both_hints_when_both_apply() -> None:
    """A field with both signals gets both prefixes, each on its own paragraph.

    ``bit0_high`` on ``light.esp32_rmt_led_strip`` is the canonical
    case — it's the named representative of the
    ``has_exactly_one_key`` constraint *and* part of the
    ``Inclusive("custom")`` group whose siblings are bit0_low /
    bit1_high / bit1_low. The user needs to see both rules to
    understand the form.
    """
    component = {
        "config_entries": [
            {"key": "chipset", "description": "Chipset."},
            {
                "key": "bit0_high",
                "description": "0-bit high.",
                "group": "custom",
            },
            {"key": "bit0_low", "description": "0-bit low.", "group": "custom"},
        ],
        "required_groups": [{"kind": "exactly_one", "keys": ["chipset", "bit0_high"]}],
    }
    _annotate_constraint_descriptions(component)
    desc = component["config_entries"][1]["description"]
    paragraphs = desc.split("\n\n")
    assert paragraphs[0].startswith("**Required — set exactly one of:**")
    assert paragraphs[1].startswith("**Set together with:**")
    assert paragraphs[-1] == "0-bit high."


def test_annotate_descriptions_recurses_into_nested_entries() -> None:
    """A nested entry's ``required_groups`` annotate its children."""
    component = {
        "config_entries": [
            {
                "key": "eap",
                "description": "EAP settings.",
                "required_groups": [
                    {"kind": "at_least_one", "keys": ["identity", "certificate"]},
                ],
                "config_entries": [
                    {"key": "identity", "description": "User identity."},
                    {
                        "key": "certificate",
                        "description": "Client cert.",
                        "group": "cert_and_key",
                    },
                    {
                        "key": "key",
                        "description": "Client key.",
                        "group": "cert_and_key",
                    },
                ],
            },
        ],
        "required_groups": [],
    }
    _annotate_constraint_descriptions(component)
    eap_inner = {e["key"]: e for e in component["config_entries"][0]["config_entries"]}
    assert eap_inner["identity"]["description"].startswith(
        "**Required — set at least one of:** `identity`, `certificate`.",
    )
    assert "**Set together with:** `key` (all-or-none)." in eap_inner["certificate"]["description"]
    # ``key`` isn't part of the required_group — only the inclusive hint.
    key_desc = eap_inner["key"]["description"]
    assert "**Required" not in key_desc
    assert "**Set together with:** `certificate` (all-or-none)." in key_desc


def test_annotate_descriptions_is_a_no_op_without_constraints() -> None:
    """Components with no constraints leave every description untouched."""
    component = {
        "config_entries": [
            {"key": "ssid", "description": "Network SSID."},
            {"key": "password", "description": "Network password."},
        ],
        "required_groups": [],
    }
    _annotate_constraint_descriptions(component)
    assert [e["description"] for e in component["config_entries"]] == [
        "Network SSID.",
        "Network password.",
    ]


def test_annotate_descriptions_handles_none_description() -> None:
    """A field with ``description=None`` still gets the prefix (no crash)."""
    component = {
        "config_entries": [
            {"key": "a", "description": None},
            {"key": "b", "description": None},
        ],
        "required_groups": [{"kind": "exactly_one", "keys": ["a", "b"]}],
    }
    _annotate_constraint_descriptions(component)
    desc = component["config_entries"][0]["description"]
    assert desc.startswith("**Required — set exactly one of:** `a`, `b`.")
    # No trailing empty paragraph from a None original.
    assert not desc.endswith("\n\n")


# ---------------------------------------------------------------------------
# Live integration tests
# ---------------------------------------------------------------------------


def test_collect_against_live_esp32_rmt_led_strip_light() -> None:
    """End-to-end: the walkers recover both constraints from the real manifest.

    Synthetic tests pin the algorithm; this one pins the integration
    against upstream. A future upstream refactor that wraps the
    timing fields in a way the walker doesn't recognise (a new
    Inclusive subclass, a custom replacement for ``has_exactly_one_key``)
    would slip past the synthetic suite — this test catches that
    class of regression.

    Asserts the *structural* property (chipset/bit0_high are the
    exactly-one constituents; the bit_* fields share an Inclusive
    group) rather than exact upstream constants so a future
    upstream rename of the group name doesn't break us — that's a
    catalog diff worth reviewing in the next nightly sync, not a
    CI failure.
    """
    pytest.importorskip("esphome.components.esp32_rmt_led_strip.light")
    from esphome.components.esp32_rmt_led_strip import light as rmt_light  # noqa: PLC0415

    manifest = _FakeManifest(rmt_light.CONFIG_SCHEMA)

    required = _collect_required_groups(manifest)
    assert () in required, (
        "esp32_rmt_led_strip.light lost its top-level has_exactly_one_key "
        "constraint — issue #924 will regress"
    )
    root_specs = required[()]
    assert any(
        spec["kind"] == "exactly_one" and "chipset" in spec["keys"] and "bit0_high" in spec["keys"]
        for spec in root_specs
    )

    inclusive = _collect_inclusive_groups(manifest)
    for bit_key in ("bit0_high", "bit0_low", "bit1_high", "bit1_low"):
        assert (bit_key,) in inclusive, f"{bit_key} lost its Inclusive marker"
    # All four timing fields share one group — the user must set
    # them as a unit or skip them entirely.
    bit_groups = {inclusive[(k,)] for k in ("bit0_high", "bit0_low", "bit1_high", "bit1_low")}
    assert len(bit_groups) == 1, f"timing fields drifted into different groups: {bit_groups}"


def test_collect_against_live_wifi_eap_nested_schema() -> None:
    """End-to-end: nested ``eap`` constraint + Inclusive markers come through.

    The wifi top-level ``eap:`` block uses both primitives at a
    nested level (``cv.has_at_least_one_key(identity, certificate)``
    plus ``cv.Inclusive(certificate/key, "certificate_and_key")``);
    pin the walker against that whole shape end-to-end so a future
    voluptuous compile-pass quirk that breaks nested-vol.All
    descent (the symptom we hit in `_walk_schema_keys`) re-surfaces
    here.
    """
    pytest.importorskip("esphome.components.wifi")
    from esphome.components import wifi  # noqa: PLC0415

    manifest = _FakeManifest(wifi.CONFIG_SCHEMA)

    required = _collect_required_groups(manifest)
    assert ("eap",) in required, "wifi.eap lost its has_at_least_one_key constraint"
    eap_specs = required[("eap",)]
    assert any(
        spec["kind"] == "at_least_one"
        and "identity" in spec["keys"]
        and "certificate" in spec["keys"]
        for spec in eap_specs
    )

    inclusive = _collect_inclusive_groups(manifest)
    # The certificate / key pair must share the same Inclusive group.
    cert_path = ("eap", "certificate")
    key_path = ("eap", "key")
    assert cert_path in inclusive
    assert key_path in inclusive
    assert inclusive[cert_path] == inclusive[key_path]


# ---------------------------------------------------------------------------
# _groups_in_all_chain / _collect_automation_registry_groups
# ---------------------------------------------------------------------------


def test_groups_in_all_chain_surfaces_validators() -> None:
    schema = cv.All(
        vol.Schema({vol.Optional("above"): float, vol.Optional("below"): float}),
        cv.has_at_least_one_key("above", "below"),
    )
    assert _groups_in_all_chain(schema) == [{"kind": "at_least_one", "keys": ["above", "below"]}]


def test_groups_in_all_chain_returns_empty_for_non_all() -> None:
    assert _groups_in_all_chain(vol.Schema({})) == []
    assert _groups_in_all_chain(None) == []


def test_groups_in_all_chain_descends_nested_all() -> None:
    inner = cv.All(vol.Schema({}), cv.has_exactly_one_key("a", "b"))
    outer = cv.All(inner, cv.has_at_most_one_key("c", "d"))
    kinds = {g["kind"] for g in _groups_in_all_chain(outer)}
    assert kinds == {"exactly_one", "at_most_one"}


def test_collect_automation_registry_groups_reads_fake_registries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = types.ModuleType("esphome.automation")
    fake.ACTION_REGISTRY = {  # type: ignore[attr-defined]
        "fake.flash": types.SimpleNamespace(
            raw_schema=cv.All(vol.Schema({}), cv.has_exactly_one_key("md5", "md5_url"))
        ),
        "fake.plain": types.SimpleNamespace(raw_schema=vol.Schema({})),
    }
    fake.CONDITION_REGISTRY = {  # type: ignore[attr-defined]
        "fake.in_range": types.SimpleNamespace(
            raw_schema=cv.All(vol.Schema({}), cv.has_at_least_one_key("above", "below"))
        ),
        "fake.no_schema": types.SimpleNamespace(),
    }
    monkeypatch.setitem(sys.modules, "esphome.automation", fake)
    assert _collect_automation_registry_groups() == {
        "action": {"fake.flash": [{"kind": "exactly_one", "keys": ["md5", "md5_url"]}]},
        "condition": {"fake.in_range": [{"kind": "at_least_one", "keys": ["above", "below"]}]},
    }


def test_collect_automation_registry_groups_empty_when_unimportable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "esphome.automation", None)
    assert _collect_automation_registry_groups() == {}


def test_collect_against_live_sensor_in_range_condition() -> None:
    """End-to-end: the live registry surfaces sensor.in_range's constraint (issue #1905)."""
    pytest.importorskip("esphome.components.sensor")
    out = _collect_automation_registry_groups()
    assert out["condition"]["sensor.in_range"] == [
        {"kind": "at_least_one", "keys": ["above", "below"]}
    ]
