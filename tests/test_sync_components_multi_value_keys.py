"""Tests for the ``_promote_multi_value_keys`` catalog post-process.

The post-process demotes ``id`` / ``*_id`` children of
``multi_value=True`` parents from Advanced to the main form, and
promotes the parent's own ``id`` to required (since cross-references
depend on it).

The upstream schema marks ``esphome.areas[].id`` and
``esphome.devices[].id`` as advanced + optional because ESPHome's id
system can auto-generate one. For repeatable nested mappings the
user is expected to set the id by hand — it's the key other rows
reference (a device's ``area_id`` points at an ``esphome.areas``
row's ``id``). Without this fix the visual editor's nested-list
renderer hides those fields behind the Advanced toggle, so a fresh
row from the Add button looks like it accepts only ``name``
(issue #434).
"""

from __future__ import annotations

from script.sync_components import (  # type: ignore[import-not-found]
    _promote_multi_value_keys,
)


def _entry(**kwargs: object) -> dict:
    """Build a config-entry dict with sensible defaults.

    Lets tests focus on the fields under test instead of repeating
    boilerplate.
    """
    base = {
        "key": "x",
        "type": "string",
        "required": False,
        "advanced": False,
        "multi_value": False,
        "config_entries": None,
    }
    base.update(kwargs)
    return base


def test_promotes_own_id_to_required_and_not_advanced() -> None:
    entries = [
        _entry(
            key="areas",
            type="nested",
            multi_value=True,
            config_entries=[
                _entry(key="name", type="string", required=True),
                _entry(key="id", type="id", required=False, advanced=True),
            ],
        ),
    ]
    _promote_multi_value_keys(entries)
    id_child = entries[0]["config_entries"][1]
    assert id_child["required"] is True
    assert id_child["advanced"] is False
    # Sibling untouched.
    name_child = entries[0]["config_entries"][0]
    assert name_child["required"] is True


def test_demotes_underscore_id_references_without_marking_required() -> None:
    # ``area_id`` on a device row references an area but the user
    # may legitimately leave it blank. Drop ``advanced`` so the
    # field is reachable without the Advanced toggle, but don't
    # force it required.
    entries = [
        _entry(
            key="devices",
            type="nested",
            multi_value=True,
            config_entries=[
                _entry(key="id", type="id", required=False, advanced=True),
                _entry(key="area_id", type="id", required=False, advanced=True),
            ],
        ),
    ]
    _promote_multi_value_keys(entries)
    id_child, area_child = entries[0]["config_entries"]
    assert id_child["required"] is True  # own id IS promoted
    assert id_child["advanced"] is False
    assert area_child["required"] is False  # ref id stays optional
    assert area_child["advanced"] is False


def test_leaves_single_value_nested_entries_alone() -> None:
    # Non-multi_value parents (regular ``key: { … }`` shape) keep
    # the upstream schema's advanced/required markings. ``ap.id``
    # on ``wifi.ap`` should still be auto-generated and advanced.
    entries = [
        _entry(
            key="ap",
            type="nested",
            multi_value=False,
            config_entries=[
                _entry(key="id", type="id", required=False, advanced=True),
            ],
        ),
    ]
    _promote_multi_value_keys(entries)
    id_child = entries[0]["config_entries"][0]
    assert id_child["required"] is False
    assert id_child["advanced"] is True


def test_recurses_into_nested_multi_value_descendants() -> None:
    # The walker should reach a ``multi_value=True`` entry that's
    # nested inside another nested entry. Future schema shapes
    # may surface a list-of-mappings under a non-list parent.
    entries = [
        _entry(
            key="outer",
            type="nested",
            multi_value=False,
            config_entries=[
                _entry(
                    key="rows",
                    type="nested",
                    multi_value=True,
                    config_entries=[
                        _entry(key="id", type="id", required=False, advanced=True),
                    ],
                ),
            ],
        ),
    ]
    _promote_multi_value_keys(entries)
    deep_id = entries[0]["config_entries"][0]["config_entries"][0]
    assert deep_id["required"] is True
    assert deep_id["advanced"] is False


def test_does_not_touch_non_id_children() -> None:
    # Only ``id`` / ``*_id`` keys are eligible. A ``name`` child
    # marked advanced (unlikely but possible) should keep its
    # markings — we don't want to flatten the entire item to the
    # main form.
    entries = [
        _entry(
            key="areas",
            type="nested",
            multi_value=True,
            config_entries=[
                _entry(key="name", type="string", advanced=True, required=True),
                _entry(key="id", type="id", required=False, advanced=True),
            ],
        ),
    ]
    _promote_multi_value_keys(entries)
    name_child = next(c for c in entries[0]["config_entries"] if c["key"] == "name")
    assert name_child["advanced"] is True


def test_re_sorts_children_so_demoted_id_lands_before_advanced_siblings() -> None:
    # ``_sort_entries`` puts non-advanced entries first, then sorts
    # within each group by ``_IMPORTANT_KEY_ORDER``. Demoting ``id``
    # from advanced leaves it stranded behind a still-advanced
    # sibling like ``comment`` if we don't re-sort. The frontend
    # renders ``config_entries`` in list order, so the row would
    # surface ``Comment (Advanced)`` ahead of ``ID``.
    entries = [
        _entry(
            key="areas",
            type="nested",
            multi_value=True,
            config_entries=[
                _entry(key="name", type="string", required=True),
                _entry(key="comment", type="string", advanced=True),
                _entry(key="id", type="id", required=False, advanced=True),
            ],
        ),
    ]
    _promote_multi_value_keys(entries)
    keys = [c["key"] for c in entries[0]["config_entries"]]
    # ``id`` (now non-advanced) must precede ``comment`` (still advanced).
    assert keys.index("id") < keys.index("comment")


def test_does_not_re_sort_when_children_already_correct() -> None:
    # If a future upstream-schema release ships ``id`` already
    # non-advanced + required, we shouldn't re-sort — the schema
    # authors may have picked a different order on purpose.
    # The walker still visits the entry but bails before flipping
    # any flag, so ``_sort_entries`` doesn't run.
    original_order = [
        _entry(key="comment", type="string", advanced=True),
        _entry(key="name", type="string", required=True),
        _entry(key="id", type="id", required=True, advanced=False),
    ]
    entries = [
        _entry(
            key="areas",
            type="nested",
            multi_value=True,
            config_entries=list(original_order),
        ),
    ]
    _promote_multi_value_keys(entries)
    assert entries[0]["config_entries"] == original_order


def test_does_not_re_sort_when_no_promotion_happened() -> None:
    # Walking past a multi_value entry without any promotable
    # children shouldn't disturb the existing order — that's
    # owned by ``_sort_entries`` upstream and should stay
    # idempotent here.
    original_order = [
        _entry(key="z_late", type="string"),
        _entry(key="a_early", type="string"),
    ]
    entries = [
        _entry(
            key="rows",
            type="nested",
            multi_value=True,
            config_entries=list(original_order),
        ),
    ]
    _promote_multi_value_keys(entries)
    assert entries[0]["config_entries"] == original_order


def test_handles_list_with_no_multi_value_entries() -> None:
    entries = [_entry(key="ssid", type="string", required=True)]
    # No multi_value entries; helper should be a no-op.
    _promote_multi_value_keys(entries)
    assert entries[0]["required"] is True
    assert entries[0]["advanced"] is False
