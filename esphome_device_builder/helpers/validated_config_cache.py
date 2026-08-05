"""
Validated-config cache (esphome's ``compiled_config``) awareness.

esphome < 2026.8 writes ``<basename>.validated.yaml`` (a raw YAML dump);
2026.8+ writes ``<basename>.validated.json`` (a ``{"v", "esphome",
"config"}`` envelope) and removes the legacy file on save. Single source
of truth for the dashboard side: path resolution, newest-cache
discovery, format-detected parsing, removal, and the remote-build
tarball member naming.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from esphome import yaml_util
from esphome.core import EsphomeError

from .storage_path import resolve_data_dir

_LOGGER = logging.getLogger(__name__)

_JSON_SUFFIX = ".validated.json"
_LEGACY_YAML_SUFFIX = ".validated.yaml"
_ENVELOPE_VERSION = 1

# Remote-build tarball member basenames; the receiver ships whichever
# file its esphome wrote.
JSON_CACHE_MEMBER_NAME = "validated.json"
LEGACY_YAML_CACHE_MEMBER_NAME = "validated.yaml"
CACHE_MEMBER_NAMES = (JSON_CACHE_MEMBER_NAME, LEGACY_YAML_CACHE_MEMBER_NAME)


def json_cache_path(configuration: str) -> Path:
    """Return the 2026.8+ JSON cache path for *configuration*."""
    return _cache_path(configuration, _JSON_SUFFIX)


def legacy_yaml_cache_path(configuration: str) -> Path:
    """Return the pre-2026.8 YAML cache path for *configuration*."""
    return _cache_path(configuration, _LEGACY_YAML_SUFFIX)


def find_validated_cache(configuration: str) -> Path | None:
    """
    Return the freshest on-disk cache for *configuration*, or ``None``.

    Newest mtime wins so an esphome up/downgrade that leaves the other
    format's file lingering never shadows the file current compiles write.
    """
    freshest: Path | None = None
    freshest_mtime = float("-inf")
    for path in (json_cache_path(configuration), legacy_yaml_cache_path(configuration)):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime > freshest_mtime:
            freshest = path
            freshest_mtime = mtime
    return freshest


def parse_validated_cache(path: Path) -> dict[Any, Any] | None:
    """
    Parse the cache at *path* (format from suffix) into a plain config dict.

    JSON caches keep their lambda sentinels as dicts (no ``Lambda``
    revival — the dashboard only reads flags); the YAML parse keeps
    ``clear_secrets=False`` so the read never wipes the process-wide
    secrets registry mid-scan. Unparseable or foreign-shaped caches
    return ``None``.
    """
    if path.name.endswith(_JSON_SUFFIX):
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            _log_unparseable(path, err)
            return None
        if not isinstance(envelope, dict) or envelope.get("v") != _ENVELOPE_VERSION:
            return None
        config = envelope.get("config")
        return config if isinstance(config, dict) else None
    try:
        config = yaml_util.load_yaml(path, clear_secrets=False)
    except EsphomeError as err:
        _log_unparseable(path, err)
        return None
    return config if isinstance(config, dict) else None


def unlink_validated_cache(configuration: str) -> None:
    """Remove both cache formats for *configuration*; no-op if absent."""
    for path in (json_cache_path(configuration), legacy_yaml_cache_path(configuration)):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            # Best-effort: a regenerable cache, debug-logged so a
            # permissions vs FS failure is distinguishable.
            _LOGGER.debug("unlink_validated_cache: unlink(%s) failed: %s", path, exc)


def member_name_for(cache_path: Path) -> str:
    """Return the tarball member basename for *cache_path*."""
    return (
        JSON_CACHE_MEMBER_NAME
        if cache_path.name.endswith(_JSON_SUFFIX)
        else (LEGACY_YAML_CACHE_MEMBER_NAME)
    )


def path_for_member(member_name: str, configuration: str) -> Path:
    """Return the local cache path a tarball *member_name* stages to."""
    if member_name == JSON_CACHE_MEMBER_NAME:
        return json_cache_path(configuration)
    if member_name == LEGACY_YAML_CACHE_MEMBER_NAME:
        return legacy_yaml_cache_path(configuration)
    msg = f"unknown validated-cache member {member_name!r}"
    raise ValueError(msg)


def sibling_cache_path(cache_path: Path, configuration: str) -> Path:
    """Return the other format's path so a stager can drop the stale sibling."""
    if cache_path.name.endswith(_JSON_SUFFIX):
        return legacy_yaml_cache_path(configuration)
    return json_cache_path(configuration)


def _cache_path(configuration: str, suffix: str) -> Path:
    """Mirror ``esphome.compiled_config``'s layout under the resolved data dir."""
    return resolve_data_dir(configuration) / "storage" / f"{Path(configuration).name}{suffix}"


def _log_unparseable(path: Path, err: Exception) -> None:
    # Class only: error detail can quote lines from the secrets-bearing cache.
    _LOGGER.debug("Validated-config cache %s did not parse (%s)", path, type(err).__name__)
