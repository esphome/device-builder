#!/usr/bin/env python3
"""
Generate ``definitions/boards/<id>/manifest.yaml`` from esphome/bluetooth-proxies.

The upstream repo (https://github.com/esphome/bluetooth-proxies) hosts one
complete, tested ESPHome config per known Bluetooth-proxy device, grouped in
vendor directories, plus a factory installer wrapper per device that this
sync ignores. Each base config becomes one imported board carrying a
``package_import_url``: creating a device from it yields a thin
``packages:`` reference to the upstream config (the dashboard-import
adoption shape), so the device tracks upstream updates on every compile
instead of vendoring the config's blocks.

The one block still mined out of the upstream YAML is ``ethernet:`` — it
stamps ``ethernet`` into ``hardware.connectivity`` (which marks the board
as providing its own network, so the create wizard skips Wi-Fi) and feeds
the pin-occupancy map. No featured entry is emitted: an addable
"Onboard Ethernet" card would vendor the pinout locally and opt the
device out of the upstream package's ethernet updates.

Imported manifests carry a ``source:`` block with
``type: bluetooth-proxies``. Hand-curated manifests and other import
sources' output are never overwritten or pruned.

Usage
-----

    python script/sync_bluetooth_proxies.py
    python script/sync_bluetooth_proxies.py --clean         # wipe cache
    python script/sync_bluetooth_proxies.py --dry-run       # no writes
    python script/sync_bluetooth_proxies.py --device <remote-id-or-stem>
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from esphome_device_builder.constants import (  # noqa: E402
    BLUETOOTH_PROXY_IMPORT_SOURCE_TYPE,
)
from script._board_import import (  # noqa: E402
    ESP32_VARIANT_DEFAULT_BOARD,
    build_esphome_block,
    build_pins,
    connectivity_for,
    emit_manifest,
    extract_ethernet,
    get_repo_revision,
    hash_content,
    prune_removed,
    safe_load_yaml,
)
from script._repo_cache import ensure_shallow_git_repo  # noqa: E402

_LOGGER = logging.getLogger("sync_bluetooth_proxies")

_BOARDS_DIR = _REPO_ROOT / "esphome_device_builder" / "definitions" / "boards"
_CACHE_ROOT = _REPO_ROOT / ".cache"
_PROXIES_CLONE_DIR = _CACHE_ROOT / "bluetooth-proxies"
_PROXIES_REPO_URL = "https://github.com/esphome/bluetooth-proxies.git"
_PROXIES_REPO_BRANCH = "main"
_PROXIES_REPO_BLOB_BASE = "https://github.com/esphome/bluetooth-proxies/blob/main"

_BOARD_ID_SUFFIX = "-bluetooth-proxy"
_DOCS_URL = "https://esphome.io/components/bluetooth_proxy"
# The ``packages:`` mapping key, matching the upstream factory configs'
# ``esphome.project.name``.
_PACKAGE_NAME = "esphome.bluetooth-proxy"
_BASE_DESCRIPTION = (
    "Known, tested Home Assistant Bluetooth proxy — imported from the official "
    "esphome/bluetooth-proxies repo. New devices reference the upstream config "
    "as a remote package, picking up its updates on every build."
)

# Hand-maintained identity per upstream config (keyed by remote_id — the
# repo-relative path without extension). A config missing here still imports
# with a name derived from its file stem, so upstream growth never stalls the
# sync; add a row to polish the new device. ``featured`` marks the
# best-performing proxies so the picker surfaces them first.
_IDENTITY: dict[str, dict[str, Any]] = {
    "olimex/olimex-esp32-poe-iso": {
        "name": "Olimex ESP32-POE-ISO Bluetooth Proxy",
        "manufacturer": "OLIMEX",
        "board": "esp32-poe-iso",
        "tags": ["poe"],
        "featured": True,
        "images": [
            "https://www.olimex.com/Products/IoT/ESP32/ESP32-POE-ISO/images/thumbs/310x230/ESP32-POE-ISO-1.jpg"
        ],
        "product_url": "https://www.olimex.com/Products/IoT/ESP32/ESP32-POE-ISO/open-source-hardware",
    },
    "seeed/seeed-esp32-poe": {
        "name": "Seeed Studio XIAO W5500 v1.2+ Bluetooth Proxy",
        "manufacturer": "Seeed Studio",
        "board": "seeed_xiao_esp32_s3_plus",
        "tags": ["compact", "poe", "usb-c"],
        "featured": True,
        "description_extra": (
            "Only XIAO W5500 Ethernet Adapters produced after November 1, 2025 "
            "(v1.2 or later) are supported."
        ),
        "images": [
            "https://files.seeedstudio.com/wiki/xiao_w5500_poe/0.jpg",
            "https://files.seeedstudio.com/wiki/xiao_w5500_poe/2.jpg",
            "https://files.seeedstudio.com/wiki/xiao_w5500_poe/3.jpg",
        ],
        "product_url": "https://www.seeedstudio.com/XIAO-W5500-Ethernet-Adapter-p-6472.html",
    },
    "gl-inet/gl-s10": {
        "name": "GL.iNet GL-S10 v2 Bluetooth Proxy",
        "manufacturer": "GL.iNet",
        "description_extra": (
            "The upstream config targets the v2.x hardware revision (IP101 "
            "Ethernet PHY); v1.0 units use a LAN8720 and need the ethernet "
            "block adjusted by hand."
        ),
        "images": [
            "https://raw.githubusercontent.com/esphome/devices.esphome.io/main/src/docs/devices/GL-iNet-GL-S10/image1.jpg"
        ],
        "product_url": "https://blakadder.com/gl-s10/",
    },
    "lilygo/lilygo-t-eth-poe": {
        "name": "LilyGO T-ETH-POE Bluetooth Proxy",
        "manufacturer": "LilyGO",
        "tags": ["poe"],
    },
    "m5stack/m5stack-atom-lite": {
        "name": "M5Stack ATOM Lite Bluetooth Proxy",
        "manufacturer": "M5Stack",
        "board": "m5stack-atom",
        "tags": ["compact", "usb-c"],
        "images": [
            "https://static-cdn.m5stack.com/resource/docs/products/core/atom_lite/atom_lite_01.webp"
        ],
    },
    "m5stack/m5stack-atom-s3": {
        "name": "M5Stack AtomS3 Bluetooth Proxy",
        "manufacturer": "M5Stack",
        "board": "m5stack-atoms3",
        "tags": ["compact", "usb-c"],
    },
    "wt32/wt32-eth01": {
        "name": "WT32-ETH01 Bluetooth Proxy",
        "manufacturer": "Wireless-Tag",
        "board": "wt32-eth01",
    },
    "esp32-generic/esp32-generic": {
        "name": "Generic ESP32 Bluetooth Proxy",
        "is_generic": True,
    },
    "esp32-generic/esp32-generic-c3": {
        "name": "Generic ESP32-C3 Bluetooth Proxy",
        "is_generic": True,
    },
    "esp32-generic/esp32-generic-c6": {
        "name": "Generic ESP32-C6 Bluetooth Proxy",
        "is_generic": True,
    },
    "esp32-generic/esp32-generic-s3": {
        "name": "Generic ESP32-S3 Bluetooth Proxy",
        "is_generic": True,
    },
}


@dataclass
class _ProxySource:
    """Raw inputs extracted from one upstream base config."""

    remote_id: str  # repo-relative path without extension, e.g. olimex/olimex-esp32-poe-iso
    stem: str
    text: str
    config: dict[str, Any]


def main() -> int:
    """Entry point: clone the upstream repo, sync, and print a report."""
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    if args.clean and _PROXIES_CLONE_DIR.is_dir():
        _LOGGER.info("Removing %s (--clean)", _PROXIES_CLONE_DIR)
        shutil.rmtree(_PROXIES_CLONE_DIR)

    repo = ensure_shallow_git_repo(
        _PROXIES_REPO_URL,
        _PROXIES_CLONE_DIR,
        _PROXIES_REPO_BRANCH,
        label="bluetooth-proxies",
        clone_fail_level=logging.ERROR,
        pull_fail_level=logging.ERROR,
    )
    if repo is None:
        return 1
    revision = get_repo_revision(repo)

    imported: list[str] = []
    skipped: list[tuple[str, str]] = []
    removed: list[str] = []
    active_remote_ids: set[str] = set()
    for src in _iter_configs(repo):
        if args.device and args.device not in (src.remote_id, src.stem):
            continue
        record, skip_reason = _make_record(src, revision)
        if skip_reason is not None:
            skipped.append((src.remote_id, skip_reason))
            continue
        if not args.dry_run and emit_manifest(record, boards_dir=_BOARDS_DIR) is None:
            skipped.append((src.remote_id, "slug collides with a board we don't own"))
            continue
        active_remote_ids.add(src.remote_id)
        imported.append(record["id"])

    if not args.dry_run and args.device is None:
        removed = prune_removed(
            active_remote_ids,
            source_type=BLUETOOTH_PROXY_IMPORT_SOURCE_TYPE,
            boards_dir=_BOARDS_DIR,
        )

    print(f"Imported: {len(imported)}")
    print(f"Skipped:  {len(skipped)}")
    print(f"Removed:  {len(removed)}")
    for remote_id, reason in skipped:
        print(f"  - {remote_id}: {reason}")
    return 0


def _iter_configs(repo: Path) -> list[_ProxySource]:
    """Collect one record per base config, skipping the factory installer wrappers."""
    out: list[_ProxySource] = []
    for vendor_dir in sorted(repo.iterdir()):
        if not vendor_dir.is_dir() or vendor_dir.name.startswith("."):
            continue
        for path in sorted(vendor_dir.glob("*.yaml")):
            # Installer wrappers, named ``<device>.factory.yaml`` upstream
            # (``-factory.yaml`` in the seeed dir).
            if path.name.endswith((".factory.yaml", "-factory.yaml")):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as err:
                # A dropped file leaves ``active_remote_ids`` and prune_removed
                # would delete its committed board — keep the skip loud.
                _LOGGER.warning("Skipping unreadable config %s: %s", path, err)
                continue
            config = safe_load_yaml(text)
            if not isinstance(config, dict):
                _LOGGER.warning("Skipping unparsable config %s", path)
                continue
            rel = path.relative_to(repo)
            out.append(
                _ProxySource(
                    remote_id=rel.with_suffix("").as_posix(),
                    stem=path.stem,
                    text=text,
                    config=config,
                )
            )
    stems: dict[str, str] = {}
    for source in out:
        if (other := stems.setdefault(source.stem, source.remote_id)) != source.remote_id:
            # Board ids derive from the bare stem; two vendors sharing one
            # would silently overwrite each other's manifest and confuse
            # pruning — fail the sync instead.
            raise SystemExit(
                f"Board id collision: {source.remote_id} and {other} share the "
                f"file stem {source.stem!r}; rename one upstream."
            )
    return out


def _make_record(src: _ProxySource, revision: str) -> tuple[dict[str, Any] | None, str | None]:
    """Build a manifest dict for one upstream config; ``(None, reason)`` on skip."""
    esp32 = src.config.get("esp32")
    if not isinstance(esp32, dict):
        return None, "no esp32 block"
    variant = esp32.get("variant")
    if not isinstance(variant, str) or variant not in ESP32_VARIANT_DEFAULT_BOARD:
        return None, f"unknown esp32 variant {variant!r}"
    if "bluetooth_proxy" not in src.config:
        return None, "no bluetooth_proxy block"
    framework = esp32.get("framework")
    framework_type = framework.get("type") if isinstance(framework, dict) else framework

    identity = _identity_for(src)
    board = identity.get("board", ESP32_VARIANT_DEFAULT_BOARD[variant])
    record: dict[str, Any] = {
        "id": f"{src.stem}{_BOARD_ID_SUFFIX}",
        "name": identity["name"],
        "description": _describe(identity),
    }
    if "manufacturer" in identity:
        record["manufacturer"] = identity["manufacturer"]
    record["esphome"] = build_esphome_block("esp32", board, variant, framework_type)
    record["package_name"] = _PACKAGE_NAME
    record["package_import_url"] = (
        f"github://esphome/bluetooth-proxies/{src.remote_id}.yaml@{_PROXIES_REPO_BRANCH}"
    )

    # The ethernet block is the one piece mined out of the upstream YAML:
    # the ``ethernet`` connectivity claim is what marks the board as
    # network-providing (the wizard skips Wi-Fi) and the pins map shows
    # the occupied GPIOs. Deliberately NOT a featured entry — an addable
    # "Onboard Ethernet" card would vendor the pinout locally, opting the
    # device out of the upstream package's ethernet updates.
    eth_entry, occupancy = extract_ethernet(src.config)
    connectivity = list(connectivity_for("esp32", variant) or [])
    if eth_entry is not None and "ethernet" not in connectivity:
        connectivity.append("ethernet")
    if connectivity:
        record["hardware"] = {"connectivity": connectivity}

    _apply_identity_extras(record, identity)
    pins = build_pins(occupancy)
    if pins:
        record["pins"] = pins
    record["source"] = {
        "type": BLUETOOTH_PROXY_IMPORT_SOURCE_TYPE,
        "remote_id": src.remote_id,
        "upstream_url": f"{_PROXIES_REPO_BLOB_BASE}/{src.remote_id}.yaml",
        **({"upstream_revision": revision} if revision else {}),
        "content_hash": hash_content(src.text),
    }
    return record, None


def _identity_for(src: _ProxySource) -> dict[str, Any]:
    """Return the hand table's row for *src*, or a derived stand-in for a new device."""
    identity = _IDENTITY.get(src.remote_id)
    if identity is not None:
        return identity
    _LOGGER.warning(
        "No identity row for %s — importing with a name derived from the stem",
        src.remote_id,
    )
    return {"name": f"{src.stem.replace('-', ' ').title()} Bluetooth Proxy"}


def _describe(identity: dict[str, Any]) -> str:
    """Compose the board description from the shared base plus any per-device note."""
    extra = identity.get("description_extra")
    return f"{_BASE_DESCRIPTION} {extra}" if extra else _BASE_DESCRIPTION


def _apply_identity_extras(record: dict[str, Any], identity: dict[str, Any]) -> None:
    """Copy the identity row's optional presentation fields onto *record*."""
    for key in ("tags", "images", "product_url"):
        if key in identity:
            record[key] = identity[key]
    record["docs_url"] = _DOCS_URL
    for flag in ("featured", "is_generic"):
        if identity.get(flag):
            record[flag] = True


def _parse_args() -> argparse.Namespace:
    """Build the CLI ArgumentParser and return parsed args."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--clean", action="store_true", help="Wipe the upstream cache first.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Run extraction but don't write any manifests."
    )
    parser.add_argument(
        "--device",
        type=str,
        default=None,
        help="Process only this remote id or file stem (e.g. gl-s10). Disables pruning.",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Print every skip reason.")
    return parser.parse_args()


if __name__ == "__main__":
    sys.exit(main())
