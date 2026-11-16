"""Refresh Zigbee endpoint platform metadata and field descriptions."""

from __future__ import annotations

import argparse
import logging
import tempfile
from pathlib import Path
from typing import Any

import orjson

from esphome_device_builder.models.components import ComponentCatalogEntry
from script._esphome_version import assert_installed_esphome
from script.sync_components import (
    _DEFINITIONS_DIR,
    _collect_platform_constraints,
    _get_esphome_loader,
    _walk_catalog_entries,
)


def main() -> int:
    """Refresh source catalog endpoint gates before packaging a matching core."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--definitions-dir", type=Path, default=_DEFINITIONS_DIR)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    count = refresh_catalog(args.definitions_dir, args.version)
    print(f"Refreshed endpoint platforms in {count} bodies using ESPHome {args.version}")
    return 0


def refresh_catalog(definitions_dir: Path, version: str) -> int:
    """Refresh endpoint gates from live manifests, validating all changes before writing."""
    assert_installed_esphome(version, what="sync_zigbee_endpoints")
    bodies_dir = definitions_dir / "components"
    paths = sorted(bodies_dir.glob("*.json"))
    if not paths:
        raise ValueError(f"No component bodies in {bodies_dir}")
    loader = _get_esphome_loader()
    if loader is None:
        raise ValueError("ESPHome loader unavailable")
    updates: dict[Path, bytes] = {}
    for path in paths:
        if path.is_symlink():
            raise ValueError(f"Refusing symlinked component body: {path.name}")
        raw = path.read_bytes()
        body = orjson.loads(raw)
        endpoints: list[dict[str, Any]] = []
        _walk_catalog_entries(
            body.get("config_entries", []),
            lambda entry, _path, endpoints=endpoints: (
                endpoints.append(entry) if _is_endpoint(entry) else None
            ),
        )
        if not endpoints:
            continue
        if body.get("id") != path.stem:
            raise ValueError(f"Component id does not match filename: {path.name}")
        before = orjson.dumps(body)
        domain, separator, stem = path.stem.partition(".")
        manifest = loader.get_platform(domain, stem) if separator else loader.get_component(domain)
        refresh_body(body, manifest)
        if before == orjson.dumps(body):
            continue
        ComponentCatalogEntry.from_dict(body)
        updates[path] = orjson.dumps(body, option=orjson.OPT_APPEND_NEWLINE)
    with tempfile.TemporaryDirectory(prefix="endpoint-refresh-", dir=definitions_dir) as temp:
        for path, data in updates.items():
            (Path(temp) / path.name).write_bytes(data)
        for path in updates:
            (Path(temp) / path.name).replace(path)
    return len(updates)


def refresh_body(body: dict[str, Any], manifest: Any) -> None:
    """Refresh platform gates and fill missing Zigbee endpoint descriptions."""
    constraints = _collect_platform_constraints(manifest)

    def visit(entry: dict[str, Any], path: tuple[str, ...]) -> None:
        if _is_endpoint(entry):
            if not (platforms := constraints.get(path)):
                raise ValueError(f"No live platform constraint for endpoint at {path!r}")
            entry["supported_platforms"] = platforms
            if not entry.get("description"):
                entry["description"] = (
                    "Manually specify the Zigbee endpoint number. "
                    "Optional; defaults to automatic numbering."
                )

    _walk_catalog_entries(body.get("config_entries", []), visit)


def _is_endpoint(entry: dict[str, Any]) -> bool:
    return entry.get("key") == "endpoint" and entry.get("depends_on_component") == "zigbee"


if __name__ == "__main__":
    raise SystemExit(main())
