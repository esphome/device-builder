"""Shared ruamel round-trip YAML factory for the automations parser/writer."""

from __future__ import annotations

from ruamel.yaml import YAML


def make_yaml() -> YAML:
    """
    Build the round-trip YAML parser/emitter the controller shares.

    Two-space mapping indent matches ESPHome's canonical layout;
    ``preserve_quotes`` keeps quoted scalars like ``"on"`` intact so
    a quoted boolean-looking string round-trips unchanged.
    """
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 4096
    return yaml
