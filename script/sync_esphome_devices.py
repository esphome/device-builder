#!/usr/bin/env python3
"""
Generate ``definitions/boards/<id>/manifest.yaml`` from devices.esphome.io.

The upstream repo (https://github.com/esphome/esphome-devices) is a
Docusaurus site whose 760+ device pages each have YAML front matter
plus an inline ``yaml`` config. This script clones the repo, walks
the device pages, and emits one ``boards/<id>/manifest.yaml`` per
device that meets the strict acceptance bar (parseable inline yaml,
identifiable board id, at least one local image, at least one
extractable featured component).

Imported manifests carry a ``source:`` block. Hand-curated manifests
in ``boards/`` (no ``source:``) are never read or written; the
sync only touches its own previous output, identified by
``source.type: esphome-devices``.

Usage
-----

    python script/sync_esphome_devices.py
    python script/sync_esphome_devices.py --clean        # wipe cache
    python script/sync_esphome_devices.py --limit 20     # debug subset
    python script/sync_esphome_devices.py --dry-run      # no writes
    python script/sync_esphome_devices.py --device <name>  # single device
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LOGGER = logging.getLogger("sync_esphome_devices")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFINITIONS_DIR = _REPO_ROOT / "esphome_device_builder" / "definitions"
_BOARDS_DIR = _DEFINITIONS_DIR / "boards"
_COMPONENTS_JSON = _DEFINITIONS_DIR / "components.json"
_CACHE_ROOT = _REPO_ROOT / ".cache"
_DEVICES_CLONE_DIR = _CACHE_ROOT / "esphome-devices"
_DEVICES_REPO_URL = "https://github.com/esphome/esphome-devices.git"
_DEVICES_REPO_BRANCH = "main"
_DEVICES_SUBDIR = Path("src/docs/devices")
_DEVICES_PAGE_BASE = "https://devices.esphome.io/devices"
_DEVICES_REPO_BLOB_BASE = "https://github.com/esphome/esphome-devices/blob/main"

_SOURCE_TYPE = "esphome-devices"

# Closed enums upstream enforces (mirror of
# esphome-devices/src/utils/validFrontmatter.ts).
_VALID_SOC_FAMILIES: frozenset[str] = frozenset({"esp32", "esp8266", "bk72xx", "rp2040", "rtl87xx"})

# Map ESP32 chip variants to a sensible default PlatformIO board id —
# used when an upstream page declares ``esp32: { variant: esp32c3 }``
# without an explicit ``board:``. Picked to match what ESPHome itself
# defaults to for each variant.
_ESP32_VARIANT_DEFAULT_BOARD: dict[str, str] = {
    "esp32": "esp32dev",
    "esp32s2": "esp32-s2-saola-1",
    "esp32s3": "esp32-s3-devkitc-1",
    "esp32c2": "esp32-c2-devkitm-1",
    "esp32c3": "esp32-c3-devkitm-1",
    "esp32c5": "esp32-c5-devkitc-1",
    "esp32c6": "esp32-c6-devkitc-1",
    "esp32c61": "esp32-c61-devkitc1",
    "esp32h2": "esp32-h2-devkitm-1",
    "esp32p4": "esp32-p4-function-ev-board",
}

# Connectivity defaults inferred from the SoC family / variant. ESP32
# variants differ on what's built in: classic + S3 + C3 + C5 + C6 +
# C61 carry both wifi + BLE; S2 has wifi only; H2 has BLE/Thread but
# no wifi; P4 has neither built in. Imports don't try to cover
# ethernet / zigbee / matter — those need explicit evidence we can't
# reliably mine from upstream.
_SOC_CONNECTIVITY: dict[str, list[str]] = {
    "esp8266": ["wifi"],
    "bk72xx": ["wifi"],
    "rp2040": ["wifi"],
    "rtl87xx": ["wifi"],
}

# Per-variant overrides for the esp32 family. ``None`` means "no
# built-in radio" (esp32p4) — we omit ``hardware.connectivity``
# entirely so the manifest doesn't claim wifi the chip can't deliver.
_ESP32_VARIANT_CONNECTIVITY: dict[str, list[str] | None] = {
    "esp32": ["wifi", "bluetooth"],
    "esp32s2": ["wifi"],
    "esp32s3": ["wifi", "bluetooth"],
    "esp32c2": ["wifi", "bluetooth"],
    "esp32c3": ["wifi", "bluetooth"],
    "esp32c5": ["wifi", "bluetooth"],
    "esp32c6": ["wifi", "bluetooth"],
    "esp32c61": ["wifi", "bluetooth"],
    "esp32h2": ["bluetooth"],
    "esp32p4": None,
}

# Top-level platform-list keys in ESPHome configs. Each list item
# carries a ``platform: <stem>`` and we project to ``<domain>.<stem>``
# in our component catalog. Mirrors the ``ComponentCategory`` entity
# domains in the catalog so we don't reject hardware that's actually
# representable (speakers, microphones, touchscreens, alarm panels).
_PLATFORM_LIST_DOMAINS: frozenset[str] = frozenset(
    {
        "alarm_control_panel",
        "binary_sensor",
        "button",
        "camera",
        "climate",
        "cover",
        "datetime",
        "display",
        "event",
        "fan",
        "light",
        "lock",
        "media_player",
        "microphone",
        "number",
        "output",
        "select",
        "sensor",
        "speaker",
        "switch",
        "text",
        "text_sensor",
        "touchscreen",
        "update",
        "valve",
    }
)

# Tag mapping from frontmatter ``type:`` to BoardTag values. Most
# upstream types map to no tag because our enum is about *hardware*
# features (relay, display, ...) while the upstream types are about
# *use* (light, plug, dimmer). Only relay-bearing devices get a tag.
_TYPE_TAG_MAP: dict[str, list[str]] = {
    "relay": ["relay"],
    "plug": ["relay", "compact"],
}

# Ecosystem tags inferred from the device name. Conservative — only
# adds tags we can be confident about from the brand name alone.
_NAME_TAG_RULES: list[tuple[str, str]] = [
    ("sonoff", "sonoff"),
    ("shelly", "shelly"),
]

# Field-name patterns that look hardware-fixed enough to lock. Anything
# else is a suggestion (the user can override in the dashboard).
_LOCKABLE_FIELD_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^pin$"),
    re.compile(r"_pin$"),
    re.compile(r"^pin_[a-z]+$"),  # pin_a, pin_b, ...
    re.compile(r"^inverted$"),
]

# Maximum images to copy per device. Some pages list 30+ photos —
# we cap to the first few so the repo doesn't bloat with PCB galleries.
_MAX_IMAGES_PER_DEVICE = 8

# Image extensions we mirror locally.
_IMAGE_EXTENSIONS: frozenset[str] = frozenset({".jpg", ".jpeg", ".png", ".webp", ".svg"})

# Fields we never lift, even if the underlying component schema lists
# them as config_entries. ``platform`` is consumed to pick the
# component itself; ``id`` is the per-instance variable name our
# dashboard generates fresh — preserving upstream's would create
# cross-instance conflicts the moment the user adds a second one.
# ``name`` is handled separately: upstream value is used when present,
# else a derived default is injected (see ``_extract_featured_components``)
# so the entity always surfaces in Home Assistant without further user
# editing.
_SKIPPED_FIELDS: frozenset[str] = frozenset({"platform", "id", "name"})

# Platform-list domains that aren't HA entities — they're referenced
# by entity wrappers (``light:`` / ``switch:`` reference an ``output:``
# entry by id) rather than surfaced directly, and emitting a top-level
# ``name:`` on one of these produces a config ESPHome rejects. Kept
# explicit so adding a new entry to ``_PLATFORM_LIST_DOMAINS`` doesn't
# silently flip its name-injection behaviour.
_NON_ENTITY_PLATFORM_DOMAINS: frozenset[str] = frozenset({"output"})

# HA-entity subset of ``_PLATFORM_LIST_DOMAINS`` — derived so a new
# entity domain added upstream doesn't get forgotten here.
_HA_ENTITY_DOMAINS: frozenset[str] = _PLATFORM_LIST_DOMAINS - _NON_ENTITY_PLATFORM_DOMAINS


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class _DeviceSource:
    """Raw inputs extracted from one upstream page before validation."""

    folder_name: str
    page_path: Path  # absolute path to index.md
    frontmatter: dict[str, Any]
    body: str
    content_hash: str
    inline_yaml: dict[str, Any] | None
    images: list[str]  # filenames relative to the device folder


@dataclass
class _SkippedDevice:
    """A device that didn't pass acceptance — kept for the report."""

    folder_name: str
    reason: str


@dataclass
class _SyncReport:
    """Aggregate result of a sync run."""

    imported: list[str] = field(default_factory=list)
    skipped: list[_SkippedDevice] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# YAML loader / dumper
# ---------------------------------------------------------------------------


class _TolerantSafeLoader(yaml.SafeLoader):
    """
    SafeLoader that swallows ESPHome-only tags as plain scalars/mappings.

    The upstream device pages happily use ``!secret``, ``!lambda``,
    ``!include``, ``!extend``, ``!remove``, ``!env_var``. The default
    SafeLoader raises on those — we just want to keep parsing the
    surrounding structure.
    """


def _passthrough_constructor(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
    """Construct *node* as the closest plain Python value, ignoring its tag."""
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    return None


for _tag in ("!secret", "!lambda", "!include", "!extend", "!remove", "!env_var"):
    _TolerantSafeLoader.add_constructor(_tag, _passthrough_constructor)


def _safe_load_yaml(text: str) -> Any:
    """Parse YAML with the tolerant loader. Returns ``None`` on error."""
    try:
        # ``_TolerantSafeLoader`` only adds passthrough constructors for
        # ESPHome-only tags — no arbitrary-object instantiation is
        # reachable, so the bandit S506 warning here is a false positive.
        return yaml.load(text, Loader=_TolerantSafeLoader)  # noqa: S506
    except yaml.YAMLError:
        return None


class _ManifestDumper(yaml.SafeDumper):
    """SafeDumper that produces stable, human-readable manifest YAML."""


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    """Render strings with embedded newlines as ``|`` literal blocks."""
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_ManifestDumper.add_representer(str, _represent_str)


def _dump_manifest(data: dict[str, Any]) -> str:
    """Render a manifest dict to YAML in our preferred style."""
    return yaml.dump(
        data,
        Dumper=_ManifestDumper,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=100,
    )


# ---------------------------------------------------------------------------
# Repo cache
# ---------------------------------------------------------------------------


def _ensure_devices_repo(*, pull: bool = True) -> Path | None:
    """
    Clone or update the esphome-devices repo. Returns its path or None.

    Mirrors the docs-repo handling in script/sync_components.py:
    shallow clone on first run, ``git pull --ff-only`` afterwards. A
    pull failure is non-fatal — we keep using whatever's on disk.

    Pass ``pull=False`` to skip the pull when the cache already exists,
    which is what the smoke test does so it inspects the same revision
    the sync just produced.
    """
    target = _DEVICES_CLONE_DIR
    if (target / ".git").exists():
        if not pull:
            return target
        result = subprocess.run(
            ["git", "-C", str(target), "pull", "-q", "--ff-only"],
            check=False,
            timeout=120,
        )
        if result.returncode != 0:
            _LOGGER.warning("git pull failed in %s — using existing snapshot", target)
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    _LOGGER.info("Cloning esphome-devices (shallow) to %s", target)
    try:
        subprocess.run(
            [
                "git",
                "clone",
                "-q",
                "--depth=1",
                "--single-branch",
                f"--branch={_DEVICES_REPO_BRANCH}",
                _DEVICES_REPO_URL,
                str(target),
            ],
            check=True,
            timeout=300,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        _LOGGER.error("Could not clone esphome-devices: %s", exc)
        return None
    return target


def _get_repo_revision(repo: Path) -> str:
    """Return the current commit SHA, or empty string on failure."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ""


# ---------------------------------------------------------------------------
# Page parsing
# ---------------------------------------------------------------------------


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_YAML_FENCE_RE = re.compile(r"```ya?ml\s*\n(.*?)\n```", re.DOTALL)
_IMAGE_REF_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+?)(?:\s+\"[^\"]*\")?\)")


def _split_frontmatter(text: str) -> tuple[dict[str, Any] | None, str]:
    """Return ``(frontmatter, body)`` from a markdown file with `---` block."""
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return None, text
    fm_text, body = match.group(1), match.group(2)
    parsed = _safe_load_yaml(fm_text)
    if not isinstance(parsed, dict):
        return None, body
    # Normalize keys — upstream has a few stray uppercased fields
    # ("Difficulty", "Made-for-esphome", ...). Lowercase everything for
    # consistent lookup.
    return {str(k).lower(): v for k, v in parsed.items()}, body


def _extract_first_yaml_block(body: str) -> dict[str, Any] | None:
    """Find the first fenced ```yaml block in *body* and parse it."""
    for match in _YAML_FENCE_RE.finditer(body):
        parsed = _safe_load_yaml(match.group(1))
        if isinstance(parsed, dict):
            return parsed
    return None


def _extract_local_images(body: str, device_dir: Path) -> list[str]:
    """Return a list of local image filenames referenced in *body*."""
    seen: list[str] = []
    seen_set: set[str] = set()
    for match in _IMAGE_REF_RE.finditer(body):
        ref = match.group(1).strip()
        if not ref or ref.startswith(("http://", "https://", "data:")):
            continue
        # Strip any leading "./" — paths in the source are relative
        # to the device folder.
        ref = ref.removeprefix("./")
        suffix = Path(ref).suffix.lower()
        if suffix not in _IMAGE_EXTENSIONS:
            continue
        # Reject path traversal; we stay strictly inside the device dir.
        if "/" in ref or "\\" in ref or ".." in ref:
            continue
        if not (device_dir / ref).is_file():
            continue
        if ref in seen_set:
            continue
        seen.append(ref)
        seen_set.add(ref)
        if len(seen) >= _MAX_IMAGES_PER_DEVICE:
            break
    return seen


def _hash_content(text: str) -> str:
    """Return the SHA-256 hex digest of *text*."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _iter_devices(repo: Path) -> Iterator[_DeviceSource]:
    """Walk *repo* and yield one record per usable device page."""
    devices_root = repo / _DEVICES_SUBDIR
    for device_dir in sorted(devices_root.iterdir()):
        if not device_dir.is_dir():
            continue
        page_path = device_dir / "index.md"
        if not page_path.is_file():
            continue
        try:
            text = page_path.read_text(encoding="utf-8")
        except OSError:
            continue
        frontmatter, body = _split_frontmatter(text)
        if frontmatter is None:
            continue
        yield _DeviceSource(
            folder_name=device_dir.name,
            page_path=page_path,
            frontmatter=frontmatter,
            body=body,
            content_hash=_hash_content(text),
            inline_yaml=_extract_first_yaml_block(body),
            images=_extract_local_images(body, device_dir),
        )


# ---------------------------------------------------------------------------
# Acceptance + record building
# ---------------------------------------------------------------------------


def _slugify(name: str) -> str:
    """Lowercase and underscore-normalize *name* for use as a board id."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower())
    return slug.strip("_")


def _gpio_number(raw: Any) -> int | None:
    """Extract a GPIO integer from any supported ESPHome pin shorthand."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        match = re.match(r"^\s*(?:GPIO)?(\d+)\s*$", raw, re.IGNORECASE)
        if match:
            return int(match.group(1))
    if isinstance(raw, dict):
        return _gpio_number(raw.get("number"))
    return None


def _normalize_pin_value(raw: Any) -> Any:
    """
    Return *raw* with any GPIO string normalized to an int.

    ``GPIO12`` → ``12`` for both the bare-int form and the rich
    ``{number: GPIO12, ...}`` form. Mode dicts are passed through.
    """
    if isinstance(raw, str):
        gpio = _gpio_number(raw)
        return gpio if gpio is not None else raw
    if isinstance(raw, dict):
        out: dict[str, Any] = {}
        for k, v in raw.items():
            if k == "number":
                num = _gpio_number(v)
                out[k] = num if num is not None else v
            else:
                out[k] = v
        return out
    return raw


def _resolve_soc(
    frontmatter: dict[str, Any], inline: dict[str, Any]
) -> tuple[str | None, dict[str, Any] | None]:
    """
    Pick the SoC family + its inline-yaml block.

    Frontmatter ``board:`` is the upstream-validated SoC family; we
    cross-check that the inline yaml has a matching block. Falls back
    to whichever family-block actually appears in the inline yaml when
    frontmatter is missing it.
    """
    fm_board = frontmatter.get("board")
    candidates: list[str] = []
    if isinstance(fm_board, str):
        for raw_token in fm_board.split(","):
            token = raw_token.strip().lower()
            if token in _VALID_SOC_FAMILIES:
                candidates.append(token)
    for family in _VALID_SOC_FAMILIES:
        if family not in candidates and family in inline:
            candidates.append(family)
    for family in candidates:
        block = inline.get(family)
        if isinstance(block, dict):
            return family, block
    return (candidates[0] if candidates else None), None


def _resolve_board_and_variant(
    soc: str, soc_block: dict[str, Any] | None
) -> tuple[str | None, str | None, str | None]:
    """
    Return ``(board, variant, framework)`` for the manifest.

    ``soc_block`` is the parsed inline-yaml block keyed under the SoC
    family (e.g. the value of ``esp32:``). For esp32, ``variant``
    falls back to a default board id when ``board:`` isn't supplied.
    """
    if soc_block is None:
        return None, None, None

    raw_board = soc_block.get("board")
    raw_variant = soc_block.get("variant")
    raw_framework = soc_block.get("framework")

    board = raw_board if isinstance(raw_board, str) else None
    # Upstream pages sometimes write the variant in uppercase (``ESP32C3``)
    # — normalize to match our enum.
    variant = raw_variant.lower() if isinstance(raw_variant, str) else None
    framework: str | None = None
    if isinstance(raw_framework, dict):
        ftype = raw_framework.get("type")
        if isinstance(ftype, str):
            framework = ftype
    elif isinstance(raw_framework, str):
        framework = raw_framework

    if soc == "esp32" and not board and variant:
        board = _ESP32_VARIANT_DEFAULT_BOARD.get(variant)

    return board, variant, framework


def _extract_featured_components(
    inline: dict[str, Any], components_index: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[int, str]]:
    """
    Build featured_components entries from inline-yaml platform lists.

    Returns ``(featured_components, gpio_occupancy)``. The occupancy
    map captures one human-readable label per GPIO referenced by an
    extracted component — used to synthesize ``pins[]`` entries.
    """
    featured: list[dict[str, Any]] = []
    gpio_occupancy: dict[int, str] = {}

    counters: dict[str, int] = {}
    for domain in sorted(inline.keys()):
        if domain not in _PLATFORM_LIST_DOMAINS:
            continue
        items = inline[domain]
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            platform = item.get("platform")
            if not isinstance(platform, str) or not platform:
                continue
            component_id = f"{domain}.{platform}"
            component = components_index.get(component_id)
            if component is None:
                continue
            fields = _extract_fields(item, component, gpio_occupancy, component_id)
            # No-field components are generic catalog entries the user
            # can add from the regular flow; including them here just
            # adds noise (sensor.uptime, switch.restart, ...). Keeping
            # only those with a real hardware-specific preset.
            if not fields:
                continue
            counters[component_id] = counters.get(component_id, 0) + 1
            local_id = f"{domain}_{platform}_{counters[component_id]}"
            # Always set ``fields.id`` so the dashboard never has to
            # auto-derive one at runtime — definitions are the source
            # of truth for what ends up in the user's YAML.
            fields["id"] = local_id
            # For HA entity platforms, also fill ``fields.name`` so the
            # entity surfaces in Home Assistant. Prefer upstream's name
            # when the page set one (and it's a simple scalar — skip
            # ``${friendly_name}`` templates the user can't sensibly
            # override); otherwise derive a default.
            if domain in _HA_ENTITY_DOMAINS:
                upstream_name = item.get("name")
                if _is_simple_scalar(upstream_name) and isinstance(upstream_name, str):
                    upstream_name = upstream_name.strip()
                else:
                    upstream_name = ""
                fields["name"] = (
                    upstream_name
                    or f"{platform.replace('_', ' ').title()} {counters[component_id]}"
                )
            entry: dict[str, Any] = {
                "id": local_id,
                "component_id": component_id,
                "fields": fields,
            }
            featured.append(entry)
    return featured, gpio_occupancy


def _extract_fields(
    inline_item: dict[str, Any],
    component: dict[str, Any],
    gpio_occupancy: dict[int, str],
    component_id: str,
) -> dict[str, Any]:
    """
    Lift hardware-fixed fields out of an inline platform-list item.

    Pin / inverted fields are written as ``locked`` presets; other
    scalars come through as bare values (unlocked suggestions).
    Per-instance fields (``id``) are skipped — the dashboard generates
    its own ids and pre-filling the upstream value would just create
    rename friction or duplicate-id collisions.
    """
    valid_keys: dict[str, dict[str, Any]] = {}
    for ce in component.get("config_entries") or []:
        key = ce.get("key")
        if isinstance(key, str):
            valid_keys[key] = ce

    out: dict[str, Any] = {}
    for fkey, fval in inline_item.items():
        if fkey in _SKIPPED_FIELDS:
            continue
        ce = valid_keys.get(fkey)
        if ce is None:
            continue
        ce_type = ce.get("type")
        if ce_type == "pin":
            normalized = _normalize_pin_value(fval)
            gpio = _gpio_number(normalized)
            if gpio is None:
                # Reference-style pins or lambdas — skip silently.
                continue
            label = _occupancy_label(inline_item, component_id)
            if gpio not in gpio_occupancy:
                gpio_occupancy[gpio] = label
            out[fkey] = {"value": normalized, "locked": True}
            continue
        if not _is_simple_scalar(fval):
            continue
        if _looks_lockable(fkey):
            out[fkey] = {"value": fval, "locked": True}
        else:
            out[fkey] = fval
    return out


def _occupancy_label(inline_item: dict[str, Any], component_id: str) -> str:
    """Build a human-readable label for a GPIO's ``occupied_by`` field."""
    for key in ("name", "id"):
        candidate = inline_item.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return f"{component_id} ({candidate.strip()})"
    return component_id


def _is_simple_scalar(value: Any) -> bool:
    """Return True if *value* is a primitive we can safely round-trip."""
    if value is None or isinstance(value, bool | int | float):
        return True
    if isinstance(value, str):
        # Keep things short — long strings are usually templated names
        # we don't want to lock the user into.
        return "${" not in value and "\n" not in value and len(value) <= 80
    return False


def _looks_lockable(field_name: str) -> bool:
    """Return True for field names that look hardware-fixed (pin, inverted, ...)."""
    return any(p.search(field_name) for p in _LOCKABLE_FIELD_PATTERNS)


def _build_pins(gpio_occupancy: dict[int, str]) -> list[dict[str, Any]]:
    """Synthesize one minimal pin entry per GPIO referenced by featured components."""
    return [
        {"gpio": gpio, "available": False, "occupied_by": gpio_occupancy[gpio]}
        for gpio in sorted(gpio_occupancy)
    ]


def _build_tags(name: str, type_field: str | None) -> list[str]:
    """Map the upstream ``type:`` and device name to the closest BoardTag values."""
    tags: list[str] = []
    if isinstance(type_field, str):
        tags.extend(_TYPE_TAG_MAP.get(type_field.strip().lower(), []))
    name_l = name.lower()
    for needle, tag in _NAME_TAG_RULES:
        if needle in name_l and tag not in tags:
            tags.append(tag)
    return tags


def _make_record(  # noqa: PLR0911 — distinct skip reasons each get their own early exit
    src: _DeviceSource,
    components_index: dict[str, dict[str, Any]],
    revision: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """
    Apply acceptance criteria + build a manifest dict.

    Returns ``(record, None)`` on success or ``(None, skip_reason)``.
    """
    fm = src.frontmatter
    title = fm.get("title")
    if not isinstance(title, str) or not title.strip():
        return None, "no frontmatter title"
    if src.inline_yaml is None:
        return None, "no parseable inline yaml"
    if not src.images:
        return None, "no local images"
    # ``type:`` is optional — only used downstream for tag inference.
    # A page without it is still importable.
    type_field = fm.get("type") if isinstance(fm.get("type"), str) else None

    soc, soc_block = _resolve_soc(fm, src.inline_yaml)
    if soc is None:
        return None, "soc family not in upstream enum"
    board, variant, framework = _resolve_board_and_variant(soc, soc_block)
    if not board:
        return None, f"no concrete board id for {soc}"

    featured, gpio_occupancy = _extract_featured_components(src.inline_yaml, components_index)
    if not featured:
        return None, "no extractable featured components"

    record: dict[str, Any] = {
        "id": _slugify(src.folder_name),
        "name": title.strip(),
        "description": "Imported from devices.esphome.io — see linked docs for community notes.",
        "esphome": _build_esphome_block(soc, board, variant, framework),
    }

    connectivity = _connectivity_for(soc, variant)
    if connectivity:
        record["hardware"] = {"connectivity": list(connectivity)}

    if src.images:
        # The loader (``_resolve_images``) resolves manifest entries
        # relative to the board directory; storing them with the
        # ``images/`` prefix preserves the upstream order. Without
        # this prefix the explicit references don't resolve and the
        # loader silently falls back to alphabetic auto-discovery.
        record["images"] = [f"images/{name}" for name in src.images]

    tags = _build_tags(src.folder_name, type_field)
    if tags:
        record["tags"] = tags

    pins = _build_pins(gpio_occupancy)
    if pins:
        record["pins"] = pins

    record["docs_url"] = f"{_DEVICES_PAGE_BASE}/{src.folder_name}/"
    project_url = fm.get("project-url")
    if isinstance(project_url, str) and project_url.startswith(("http://", "https://")):
        record["product_url"] = project_url

    record["featured_components"] = featured

    record["source"] = _build_source_block(src.folder_name, revision, src.content_hash)

    return record, None


def _build_esphome_block(
    soc: str, board: str, variant: str | None, framework: str | None
) -> dict[str, Any]:
    """Compose the manifest's ``esphome:`` block, omitting empty optional fields."""
    out: dict[str, Any] = {"platform": soc, "board": board}
    if variant:
        out["variant"] = variant
    if framework in ("arduino", "esp-idf"):
        out["framework"] = framework
    return out


def _connectivity_for(soc: str, variant: str | None) -> list[str] | None:
    """Return the built-in radio mix for *soc*/*variant*, or ``None`` for none."""
    if soc == "esp32":
        # Variants without an explicit override fall through to the
        # classic esp32 default (wifi + bluetooth).
        return _ESP32_VARIANT_CONNECTIVITY.get(variant or "esp32", ["wifi", "bluetooth"])
    return _SOC_CONNECTIVITY.get(soc)


def _build_source_block(folder_name: str, revision: str, content_hash: str) -> dict[str, Any]:
    """Compose the manifest's ``source:`` block (origin + drift-detection metadata)."""
    block: dict[str, Any] = {
        "type": _SOURCE_TYPE,
        "remote_id": folder_name,
        "upstream_url": f"{_DEVICES_REPO_BLOB_BASE}/{_DEVICES_SUBDIR.as_posix()}/"
        f"{folder_name}/index.md",
    }
    if revision:
        block["upstream_revision"] = revision
    block["content_hash"] = content_hash
    return block


# ---------------------------------------------------------------------------
# Emit + prune
# ---------------------------------------------------------------------------


def _emit_manifest(record: dict[str, Any], src: _DeviceSource) -> Path | None:
    """
    Write ``boards/<id>/manifest.yaml`` and refresh the images.

    Skips with a warning when *target_dir* already holds a non-imported
    manifest (slug collision with a hand-curated board). Otherwise
    cleans the existing ``images/`` subdir before copying so an
    upstream image-set shrink doesn't leave stale files behind.
    """
    target_dir = _BOARDS_DIR / record["id"]
    if not _is_writable_target(target_dir):
        _LOGGER.warning(
            "Skipping %s — slug collides with a hand-curated board (no source.type)",
            record["id"],
        )
        return None
    target_dir.mkdir(parents=True, exist_ok=True)

    images_dir = target_dir / "images"
    if images_dir.is_dir():
        # Wipe the directory first so a removed upstream image
        # disappears from the local copy too. Only the ``images/``
        # subdir is touched — the manifest itself is overwritten
        # below in a single write.
        shutil.rmtree(images_dir)
    if src.images:
        images_dir.mkdir()
        device_dir = src.page_path.parent
        for image_name in src.images:
            src_path = device_dir / image_name
            if src_path.is_file():
                shutil.copy2(src_path, images_dir / image_name)

    manifest_path = target_dir / "manifest.yaml"
    manifest_path.write_text(_dump_manifest(record), encoding="utf-8")
    return target_dir


def _is_writable_target(target_dir: Path) -> bool:
    """Return True when the sync owns *target_dir* (or it doesn't exist yet)."""
    manifest = target_dir / "manifest.yaml"
    if not manifest.is_file():
        return True
    is_imported, _ = _is_imported_manifest(manifest)
    return is_imported


def _is_imported_manifest(manifest_path: Path) -> tuple[bool, str | None]:
    """Return ``(is_imported, remote_id)`` for an existing board manifest."""
    try:
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return False, None
    if not isinstance(data, dict):
        return False, None
    source = data.get("source")
    if not isinstance(source, dict) or source.get("type") != _SOURCE_TYPE:
        return False, None
    remote_id = source.get("remote_id")
    return True, remote_id if isinstance(remote_id, str) else None


def _prune_removed(active_remote_ids: set[str]) -> list[str]:
    """Delete boards/<id>/ for any imported manifest no longer upstream."""
    removed: list[str] = []
    if not _BOARDS_DIR.is_dir():
        return removed
    for child in sorted(_BOARDS_DIR.iterdir()):
        if not child.is_dir():
            continue
        manifest = child / "manifest.yaml"
        if not manifest.is_file():
            continue
        is_imported, remote_id = _is_imported_manifest(manifest)
        if not is_imported:
            continue
        if remote_id and remote_id in active_remote_ids:
            continue
        shutil.rmtree(child)
        removed.append(child.name)
    return removed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _load_components_index() -> dict[str, dict[str, Any]]:
    """Index ``definitions/components.json`` by component id."""
    if not _COMPONENTS_JSON.is_file():
        raise SystemExit(f"{_COMPONENTS_JSON} not found — run script/sync_components.py first.")
    raw = json.loads(_COMPONENTS_JSON.read_text(encoding="utf-8"))
    return {comp["id"]: comp for comp in raw.get("components", []) if comp.get("id")}


def _parse_args() -> argparse.Namespace:
    """Build the CLI ArgumentParser and return parsed args."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Wipe the upstream cache before pulling.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after N successful imports (debugging). Disables pruning.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run extraction but don't write any manifests or images.",
    )
    parser.add_argument(
        "--device",
        type=str,
        default=None,
        help="Process only this upstream folder name (e.g. Sonoff-BASIC-R2-v1.4).",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print every skip reason.",
    )
    return parser.parse_args()


def main() -> int:
    """Entry point: clone the upstream repo, sync, and print a report."""
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    if args.clean and _DEVICES_CLONE_DIR.is_dir():
        _LOGGER.info("Removing %s (--clean)", _DEVICES_CLONE_DIR)
        shutil.rmtree(_DEVICES_CLONE_DIR)

    repo = _ensure_devices_repo()
    if repo is None:
        return 1
    revision = _get_repo_revision(repo)

    components_index = _load_components_index()

    report = _SyncReport()
    active_remote_ids: set[str] = set()

    for src in _iter_devices(repo):
        if args.device and src.folder_name != args.device:
            continue
        record, skip_reason = _make_record(src, components_index, revision)
        if skip_reason is not None:
            report.skipped.append(_SkippedDevice(src.folder_name, skip_reason))
            if args.verbose:
                _LOGGER.debug("skip %s: %s", src.folder_name, skip_reason)
            continue
        if not args.dry_run and _emit_manifest(record, src) is None:
            report.skipped.append(
                _SkippedDevice(src.folder_name, "slug collides with hand-curated board")
            )
            continue
        active_remote_ids.add(src.folder_name)
        report.imported.append(record["id"])
        if args.limit is not None and len(report.imported) >= args.limit:
            break

    # Pruning is dangerous when --limit / --device is in effect, since
    # we haven't actually visited the rest of the upstream tree.
    if not args.dry_run and args.limit is None and args.device is None:
        report.removed = _prune_removed(active_remote_ids)

    _print_report(report, args.verbose)
    return 0


def _print_report(report: _SyncReport, verbose: bool) -> None:
    """Pretty-print *report* to stdout."""
    print(f"Imported: {len(report.imported)}")
    print(f"Skipped:  {len(report.skipped)}")
    print(f"Removed:  {len(report.removed)}")

    if report.skipped:
        from collections import Counter

        reasons = Counter(s.reason for s in report.skipped)
        print("\nTop skip reasons:")
        for reason, count in reasons.most_common(10):
            print(f"  {count:>4}  {reason}")

    if verbose and report.skipped:
        print("\nAll skips:")
        for s in report.skipped:
            print(f"  - {s.folder_name}: {s.reason}")


if __name__ == "__main__":
    sys.exit(main())
