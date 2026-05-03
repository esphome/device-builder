"""Tests for ``helpers/json.py`` orjson wrappers.

Most of the helpers are thin enough that their behavior is
self-evident from a glance, but ``dumps_str_non_str_keys`` flips
an orjson option and only one endpoint depends on the result —
worth a dedicated test so a future "let's drop the unused option"
cleanup doesn't silently regress legacy ``/json-config``.
"""

from __future__ import annotations

import pytest

from esphome_device_builder.helpers.json import (
    dumps,
    dumps_str_non_str_keys,
    loads,
)


class _StrSubclass(str):
    """Stand-in for ESPHome's ``EStr`` (a ``str`` subclass)."""


def test_dumps_rejects_str_subclass_keys() -> None:
    """Plain ``dumps`` keeps orjson's strict default.

    Non-exact-``str`` keys raise ``TypeError``. The strict default
    catches the common bug of leaking a non-string key into a JSON
    response, so this test pins it — the legacy ``/json-config``
    endpoint reaches for ``dumps_str_non_str_keys`` precisely
    because plain ``dumps`` would refuse its EStr-keyed input.
    """
    with pytest.raises(TypeError, match="Dict key must be str"):
        dumps({_StrSubclass("hello"): "world"})


def test_dumps_str_non_str_keys_serialises_str_subclass_keys() -> None:
    """``dumps_str_non_str_keys`` permits ``str``-subclass keys.

    ESPHome's ``yaml_util.load_yaml`` returns dicts whose keys are
    ``EStr`` (a ``str`` subclass carrying source-position info), and
    legacy ``/json-config`` ships those dicts straight to HA. The
    helper has to round-trip them as plain JSON strings.
    """
    payload = {_StrSubclass("esphome"): {_StrSubclass("name"): "kitchen"}}
    out = dumps_str_non_str_keys(payload)
    assert isinstance(out, str)
    assert loads(out) == {"esphome": {"name": "kitchen"}}


def test_dumps_str_non_str_keys_serialises_plain_str_keys() -> None:
    """Plain ``str`` keys round-trip through the permissive helper.

    The option name is ``OPT_NON_STR_KEYS``, and the spelling makes
    it sound like it *replaces* string-key handling — it doesn't, it
    adds non-``str`` keys to what's already accepted. Pin that so a
    future reader doesn't reach for a separate helper for the
    common case.
    """
    out = dumps_str_non_str_keys({"plain": 1, "nested": {"x": 2}})
    assert isinstance(out, str)
    assert loads(out) == {"plain": 1, "nested": {"x": 2}}
