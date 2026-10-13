"""Tolerant PyYAML loading for raw device text: ``!secret`` kept as a marker, other tags ignored."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml

from ..yaml import FastestSafeLoader


@dataclass(frozen=True, slots=True)
class SecretRef:
    """Marker for an unresolved ``!secret <name>`` reference.

    Value equality matters: identity eq would defeat the reload event
    suppression and the mqtt nudge gate for ``!secret`` blocks.
    """

    name: str


def load_yaml_tolerant(yaml_content: str) -> Any | None:
    """Parse *yaml_content* tolerantly: ``!secret`` kept, other tags ignored, ``None`` on error."""
    try:
        # _TolerantYamlLoader subclasses FastestSafeLoader (libyaml's
        # CSafeLoader when available, the pure-Python SafeLoader
        # otherwise; both are safe). The custom !secret constructor
        # only emits a marker class, never instantiates arbitrary types.
        return yaml.load(yaml_content, Loader=_TolerantYamlLoader)  # noqa: S506
    except yaml.YAMLError:
        return None


class _TolerantYamlLoader(FastestSafeLoader):
    """SafeLoader that captures ``!secret`` and ignores other custom tags."""


def _construct_secret(loader: yaml.Loader, node: yaml.ScalarNode) -> SecretRef:
    return SecretRef(loader.construct_scalar(node))


def _ignore_unknown_tag(_loader: yaml.Loader, _tag_suffix: str, _node: yaml.Node) -> None:
    return None


_TolerantYamlLoader.add_constructor("!secret", _construct_secret)
_TolerantYamlLoader.add_multi_constructor("!", _ignore_unknown_tag)
