"""Shared YAML manifest reader for the definition scripts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

# Local libyaml-or-fallback alias, deliberately NOT imported from
# esphome_device_builder.helpers.yaml: that package imports esphome, and
# the validate-definitions pre-commit hook runs in envs without it.
try:
    _FastestSafeLoader: type = yaml.CSafeLoader
except AttributeError:  # pragma: no cover
    _FastestSafeLoader = yaml.SafeLoader


class ManifestError(Exception):
    """A manifest failed to parse as YAML or wasn't a mapping."""


def load_manifest_dict(path: Path) -> dict[str, Any]:
    """
    Read and parse *path* as a YAML mapping.

    Raises :class:`ManifestError` (carrying a human reason) on a YAML parse
    error or a non-mapping document; an ``OSError`` from the read propagates
    unchanged so callers can tell "can't read" apart from "bad content".
    """
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_FastestSafeLoader)  # noqa: S506
    except yaml.YAMLError as exc:
        raise ManifestError(f"invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError("manifest is not a YAML mapping")
    return data
