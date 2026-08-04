"""Tolerant ``mqtt:`` block extraction shared by the scanner and the MQTT coordinator."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from ...models.devices import DeviceMqttExtract
from ..yaml import FastestSafeLoader
from ._parsing import _extract_resolved_substitutions


class SecretRef:
    """Marker for an unresolved ``!secret <name>`` reference."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name


def extract_mqtt_block(yaml_content: str) -> tuple[dict[str, Any] | None, dict[str, str]]:
    """
    Tolerant-parse *yaml_content*; return its ``mqtt:`` dict and substitutions.

    ``!secret`` values stay as :class:`SecretRef` markers; other custom
    tags load as ``None``. Returns ``(None, {})`` when the YAML fails to
    parse or has no ``mqtt:`` mapping.
    """
    try:
        # _TolerantYamlLoader subclasses FastestSafeLoader (libyaml's
        # CSafeLoader when available, the pure-Python SafeLoader
        # otherwise — both are safe). The custom !secret constructor
        # only emits a marker class, never instantiates arbitrary types.
        data = yaml.load(yaml_content, Loader=_TolerantYamlLoader)  # noqa: S506
    except yaml.YAMLError:
        return None, {}
    if not isinstance(data, dict):
        return None, {}
    mqtt = data.get("mqtt")
    if not isinstance(mqtt, dict):
        return None, {}
    return mqtt, _extract_resolved_substitutions(data)


def build_mqtt_extract(
    yaml_content: str,
    resolved_config: dict[str, Any] | None,
    yaml_stat: os.stat_result,
    secrets_mtime: float,
    resolved_substitutions: dict[str, str],
) -> DeviceMqttExtract:
    """Build the scan-time extraction the MQTT coordinator consumes."""
    main_block, main_subs = extract_mqtt_block(yaml_content)
    resolved_block = resolved_config.get("mqtt") if isinstance(resolved_config, dict) else None
    if not isinstance(resolved_block, dict):
        resolved_block = None
    return DeviceMqttExtract(
        yaml_mtime=yaml_stat.st_mtime,
        yaml_size=yaml_stat.st_size,
        secrets_mtime=secrets_mtime,
        main_block=main_block,
        main_substitutions=main_subs,
        resolved_block=resolved_block,
        resolved_substitutions=resolved_substitutions if resolved_block is not None else {},
    )


def safe_mtime(path: Path) -> float:
    """Return *path*'s mtime, or ``0.0`` when the file is missing."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


class _TolerantYamlLoader(FastestSafeLoader):
    """SafeLoader that captures ``!secret`` and ignores other custom tags."""


def _construct_secret(loader: yaml.Loader, node: yaml.ScalarNode) -> SecretRef:
    return SecretRef(loader.construct_scalar(node))


def _ignore_unknown_tag(_loader: yaml.Loader, _tag_suffix: str, _node: yaml.Node) -> None:
    return None


_TolerantYamlLoader.add_constructor("!secret", _construct_secret)
_TolerantYamlLoader.add_multi_constructor("!", _ignore_unknown_tag)
