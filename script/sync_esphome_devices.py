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
_COMPONENTS_INDEX_JSON = _DEFINITIONS_DIR / "components.index.json"
_COMPONENTS_BODIES_DIR = _DEFINITIONS_DIR / "components"
_CACHE_ROOT = _REPO_ROOT / ".cache"
_DEVICES_CLONE_DIR = _CACHE_ROOT / "esphome-devices"
_DEVICES_REPO_URL = "https://github.com/esphome/esphome-devices.git"
_DEVICES_REPO_BRANCH = "main"
_DEVICES_SUBDIR = Path("src/docs/devices")
_DEVICES_PAGE_BASE = "https://devices.esphome.io/devices"
_DEVICES_REPO_BLOB_BASE = "https://github.com/esphome/esphome-devices/blob/main"
_DEVICES_REPO_RAW_BASE = "https://raw.githubusercontent.com/esphome/esphome-devices/main"

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

# Strings that mark "user must fill this in" placeholders in upstream
# YAML. Lifting them as featured-component presets would create an
# entity that compiles but can't actually run — better to skip the
# whole component and let the user add the underlying catalog entry
# manually. Match is case-insensitive and substring-anchored.
_PLACEHOLDER_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bfill\s*in\b", re.IGNORECASE),
    re.compile(r"\breplace\s*me\b", re.IGNORECASE),
    re.compile(r"<\s*replaceme\s*>", re.IGNORECASE),
    re.compile(r"<[A-Z_][A-Z0-9_]*>"),  # <UNKNOWN>, <ADDRESS>, ...
    re.compile(r"\byour[\s_-]+(key|address|token|id)\b", re.IGNORECASE),
]

# Template substitutions like ``${friendly_name}`` that upstream pages
# resolve at runtime via ``substitutions:``. We don't carry the
# substitutions block forward, so anything still containing one is
# unsafe to surface as a preset value or as an ``occupied_by`` label.
_TEMPLATE_VAR_RE = re.compile(r"\$\{[^}]*\}")

# Platforms we never lift, regardless of which domain hosts them. The
# ``template`` family (``switch.template``, ``binary_sensor.template``,
# ...) and the ``copy`` family both rely on user-provided lambdas or
# id references for their actual behaviour — without those we'd emit a
# featured component that compiles but does nothing.
_SKIPPED_PLATFORMS: frozenset[str] = frozenset({"template", "copy"})

# Top-level inline-yaml keys whose presence means the upstream YAML's
# behaviour comes from a lambda we can't represent in a preset. When
# any of these appears on a featured-component item we drop the whole
# item rather than emit a static skeleton.
_LAMBDA_BEHAVIOUR_KEYS: frozenset[str] = frozenset({"lambda", "write_lambda"})

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
    # Upstream pages occasionally ship a ``<REPLACEME>`` placeholder
    # where a real PlatformIO board id should be — those configs would
    # never compile, so treat them the same as a missing board.
    if board is not None and _is_placeholder_value(board):
        board = None
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


@dataclass
class _Candidate:
    """One inline-yaml item that survived filtering, ready to render."""

    item: dict[str, Any]
    domain: str
    platform: str
    component_id: str
    component: dict[str, Any]
    local_id: str
    fields: dict[str, Any]
    counter: int  # 1-based position among kept entries with the same component_id


def _extract_featured_components(
    inline: dict[str, Any], components_index: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[int, str]]:
    """
    Build ``featured_components`` + ``featured_bundles`` from inline-yaml platform lists.

    Returns ``(featured_components, featured_bundles, gpio_occupancy)``.
    The occupancy map captures one human-readable label per GPIO
    referenced by an extracted component — used to synthesize the
    manifest's ``pins[]`` block.

    Pass 1 walks the inline yaml, applies safety filters (placeholder
    sentinels, lambda-driven items, ``*.template`` platforms) and
    pre-assigns each survivor a local id — preferring the upstream
    ``id:`` value so cross-component references like
    ``light.rgbct.red: red_output`` still resolve in the user's YAML.
    Pass 2 rewrites those references through the upstream→local id
    map and emits the final entries; bundles are derived from the
    same map so the dashboard can add a multi-component setup
    (e.g. RGB(W) light + its PWM outputs) as a single click.
    """
    candidates: list[_Candidate] = []
    used_ids: set[str] = set()
    counters: dict[str, int] = {}
    gpio_occupancy: dict[int, str] = {}

    for domain in sorted(inline.keys()):
        if domain not in _PLATFORM_LIST_DOMAINS:
            continue
        items = inline[domain]
        if not isinstance(items, list):
            continue
        for item in items:
            candidate = _build_candidate(
                item, domain, components_index, gpio_occupancy, used_ids, counters
            )
            if candidate is None:
                continue
            used_ids.add(candidate.local_id)
            candidates.append(candidate)

    survivors = _select_survivors(candidates)
    id_map = _build_id_map(survivors)
    featured = [_finalize_entry(c, id_map) for c in survivors]
    bundles = _build_bundles(survivors, id_map)
    return featured, bundles, gpio_occupancy


def _select_survivors(candidates: list[_Candidate]) -> list[_Candidate]:
    """
    Pick the candidates that should land in the manifest.

    Initial survivors are the candidates whose tentative entry — built
    against the unfiltered id map — already carries a useful preset
    (own scalar/pin field, or an id ref that resolves to a sibling).
    From there we walk the id-reference graph upward, pulling in any
    producers a survivor depends on. So an RGBW bulb keeps its PWM
    outputs even when their pins use SoC-specific names we can't
    parse, while standalone components with no presets and no
    consumers get pruned as no-op skeletons.
    """
    full_id_map = _build_id_map(candidates)
    by_local: dict[str, _Candidate] = {c.local_id: c for c in candidates}

    survivor_locals: set[str] = set()
    for cand in candidates:
        entry = _finalize_entry(cand, full_id_map)
        if _entry_has_useful_preset(cand, entry):
            survivor_locals.add(cand.local_id)

    while True:
        added = False
        for local_id in list(survivor_locals):
            cand = by_local[local_id]
            for target in _id_ref_targets(cand, full_id_map):
                if target not in survivor_locals:
                    survivor_locals.add(target)
                    added = True
        if not added:
            break

    return [c for c in candidates if c.local_id in survivor_locals]


def _entry_has_useful_preset(candidate: _Candidate, entry: dict[str, Any]) -> bool:
    """
    Return True when *entry* carries a real preset beyond the auto-injected ``id`` / ``name``.

    Lets ``_select_survivors`` distinguish skeleton components (no
    real fields) from consumers whose only contribution is a resolved
    ``output:`` reference — both have empty pass-1 ``fields`` but only
    the latter is worth keeping in the manifest.
    """
    fields = entry["fields"]
    auto_keys = {"id", "name"} if candidate.domain in _HA_ENTITY_DOMAINS else {"id"}
    return any(key not in auto_keys for key in fields)


def _id_ref_targets(cand: _Candidate, id_map: dict[str, str]) -> Iterator[str]:
    """Yield each kept-sibling local id referenced by *cand*'s ``type: "id"`` fields."""
    valid_keys = {
        ce.get("key"): ce
        for ce in cand.component.get("config_entries") or []
        if isinstance(ce.get("key"), str)
    }
    for fkey, fval in cand.item.items():
        if fkey in _SKIPPED_FIELDS:
            continue
        ce = valid_keys.get(fkey)
        if ce is None or ce.get("type") != "id":
            continue
        if not isinstance(fval, str):
            continue
        mapped = id_map.get(fval)
        if mapped is not None:
            yield mapped


def _build_candidate(  # noqa: PLR0911 — distinct skip reasons each get their own early exit
    item: Any,
    domain: str,
    components_index: dict[str, dict[str, Any]],
    gpio_occupancy: dict[int, str],
    used_ids: set[str],
    counters: dict[str, int],
) -> _Candidate | None:
    """
    Turn one upstream inline-yaml entry into a ``_Candidate`` or skip it.

    Applies the same safety filters as before — non-mapping items,
    blank platform, ``*.template`` / ``*.copy`` platforms, top-level
    ``lambda:``, components missing from our catalog, placeholder field
    values, and items whose hardware-fixed fields all got filtered out.
    Returns ``None`` for any of those; otherwise records the per-item
    GPIO occupancy and assigns the local id.
    """
    if not isinstance(item, dict):
        return None
    platform = item.get("platform")
    if not isinstance(platform, str) or not platform:
        return None
    if platform in _SKIPPED_PLATFORMS:
        return None
    if any(key in item for key in _LAMBDA_BEHAVIOUR_KEYS):
        return None
    component_id = f"{domain}.{platform}"
    component = components_index.get(component_id)
    if component is None:
        return None
    local_occupancy: dict[int, str] = {}
    fields = _extract_fields(item, component, local_occupancy, component_id)
    # ``None`` means an unfillable placeholder ("(FILL IN ...)").
    if fields is None:
        return None
    # ``{}`` is fine when the inline item carries a ``type: "id"`` ref
    # we'll resolve in pass 2 (e.g. ``light.binary`` consuming an
    # ``output.gpio``); otherwise it means no hardware-specific value
    # at all and the entry would be a no-op skeleton.
    if not fields and not _has_id_reference_fields(item, component):
        return None
    gpio_occupancy.update(local_occupancy)
    counters[component_id] = counters.get(component_id, 0) + 1
    local_id = _assign_local_id(item, domain, platform, used_ids, counters[component_id])
    return _Candidate(
        item=item,
        domain=domain,
        platform=platform,
        component_id=component_id,
        component=component,
        local_id=local_id,
        fields=fields,
        counter=counters[component_id],
    )


def _assign_local_id(
    item: dict[str, Any],
    domain: str,
    platform: str,
    used_ids: set[str],
    counter: int,
) -> str:
    """
    Pick a local id, preferring the sanitized upstream ``id:`` field.

    Falls back to ``<domain>_<platform>_<counter>`` when no upstream
    id exists, the value can't be sanitized to a valid local id, the
    candidate equals the bare domain (validate_definitions flags
    ``id: light`` on a ``light.tuya`` as a domain clash), or it
    collides with one already assigned to a sibling on this board.
    """
    upstream_id = item.get("id")
    if isinstance(upstream_id, str):
        sanitized = _sanitize_local_id(upstream_id)
        if sanitized and sanitized != domain and sanitized not in used_ids:
            return sanitized
    return f"{domain}_{platform}_{counter}"


def _has_id_reference_fields(item: dict[str, Any], component: dict[str, Any]) -> bool:
    """Return True when *item* has a ``type: "id"`` field defined by *component*."""
    valid_keys = {
        ce.get("key"): ce
        for ce in component.get("config_entries") or []
        if isinstance(ce.get("key"), str)
    }
    for fkey, fval in item.items():
        if fkey in _SKIPPED_FIELDS:
            continue
        ce = valid_keys.get(fkey)
        if ce is None or ce.get("type") != "id":
            continue
        if isinstance(fval, str) and fval:
            return True
    return False


def _sanitize_local_id(raw: str) -> str:
    """
    Normalize *raw* into a valid manifest local id, or empty on failure.

    Local ids must match ``^[a-z][a-z0-9_]*$`` (the schema's component
    + bundle pattern). Lowercases, replaces non-id characters with
    underscores, collapses runs, trims, and rejects values that don't
    start with a letter after cleanup.
    """
    cleaned = re.sub(r"[^a-z0-9_]", "_", raw.lower())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned or not cleaned[0].isalpha():
        return ""
    return cleaned


def _build_id_map(candidates: list[_Candidate]) -> dict[str, str]:
    """Map each kept candidate's upstream ``id:`` to its assigned local id."""
    out: dict[str, str] = {}
    for cand in candidates:
        upstream_id = cand.item.get("id")
        if isinstance(upstream_id, str) and upstream_id:
            out.setdefault(upstream_id, cand.local_id)
    return out


def _finalize_entry(candidate: _Candidate, id_map: dict[str, str]) -> dict[str, Any]:
    """
    Render one ``_Candidate`` as a ``featured_components`` dict.

    Resolves cross-component ``type: "id"`` references through *id_map*
    (dropping refs whose target wasn't kept), then injects the standard
    ``id`` and (for HA entity domains) ``name`` fields.
    """
    fields = dict(candidate.fields)
    _apply_id_references(fields, candidate.item, candidate.component, id_map)
    fields["id"] = candidate.local_id
    if candidate.domain in _HA_ENTITY_DOMAINS:
        fields["name"] = _clean_entity_name(candidate.item) or (
            f"{candidate.platform.replace('_', ' ').title()} {candidate.counter}"
        )
    return {
        "id": candidate.local_id,
        "component_id": candidate.component_id,
        "fields": fields,
    }


def _apply_id_references(
    fields: dict[str, Any],
    inline_item: dict[str, Any],
    component: dict[str, Any],
    id_map: dict[str, str],
) -> None:
    """
    Add ``type: "id"`` reference fields to *fields*, remapped via *id_map*.

    The dashboard regenerates per-instance ids, so the upstream value
    (``output: red_output``) only resolves when its target was also
    kept as a featured component on the same board. Refs to dropped
    components are silently omitted — the user picks a real target
    when adding the consumer.
    """
    valid_keys = {
        ce.get("key"): ce
        for ce in component.get("config_entries") or []
        if isinstance(ce.get("key"), str)
    }
    for fkey, fval in inline_item.items():
        if fkey in _SKIPPED_FIELDS:
            continue
        ce = valid_keys.get(fkey)
        if ce is None or ce.get("type") != "id":
            continue
        if not isinstance(fval, str):
            continue
        mapped = id_map.get(fval)
        if mapped is not None:
            fields[fkey] = mapped


def _build_bundles(candidates: list[_Candidate], id_map: dict[str, str]) -> list[dict[str, Any]]:
    """
    Derive ``featured_bundles`` from id-reference dependencies.

    For each candidate that consumes one or more sibling components
    via ``type: "id"`` fields (e.g. ``light.rgbct`` referencing the
    PWM outputs that drive its colour channels), emit a bundle whose
    members are the dependency ids followed by the consumer itself —
    so the dashboard adds them in the right order in one shot.
    """
    bundles: list[dict[str, Any]] = []
    used_bundle_ids: set[str] = set()
    for cand in candidates:
        members = _bundle_members_for(cand, id_map)
        if len(members) < 2:
            continue
        bundle_id = _bundle_id_for(cand, used_bundle_ids)
        used_bundle_ids.add(bundle_id)
        bundles.append(
            {
                "id": bundle_id,
                "name": _bundle_name_for(cand),
                "component_ids": members,
            }
        )
    return bundles


def _bundle_members_for(cand: _Candidate, id_map: dict[str, str]) -> list[str]:
    """
    List the local ids a consumer's bundle should add, dependencies first.

    Walks the consumer's inline-yaml fields, collects every ``type:
    "id"`` value that resolves through *id_map*, then appends the
    consumer's own local id. Order is preserved and duplicates are
    dropped — the dashboard adds members one by one and the consumer
    must come last so its ``output:`` references already exist.
    """
    valid_keys = {
        ce.get("key"): ce
        for ce in cand.component.get("config_entries") or []
        if isinstance(ce.get("key"), str)
    }
    members: list[str] = []
    seen: set[str] = set()
    for fkey, fval in cand.item.items():
        if fkey in _SKIPPED_FIELDS:
            continue
        ce = valid_keys.get(fkey)
        if ce is None or ce.get("type") != "id":
            continue
        if not isinstance(fval, str):
            continue
        mapped = id_map.get(fval)
        if mapped is not None and mapped not in seen:
            members.append(mapped)
            seen.add(mapped)
    if cand.local_id not in seen:
        members.append(cand.local_id)
    return members


def _bundle_id_for(cand: _Candidate, used: set[str]) -> str:
    """Return a bundle id derived from the consumer, unique within the board."""
    base = f"{cand.local_id}_setup"
    if base not in used:
        return base
    counter = 2
    while f"{base}_{counter}" in used:
        counter += 1
    return f"{base}_{counter}"


def _bundle_name_for(cand: _Candidate) -> str:
    """Pick a human-readable bundle name from the consumer's upstream item."""
    cleaned = _clean_entity_name(cand.item)
    if cleaned:
        return f"{cleaned} (full setup)"
    return f"{cand.platform.replace('_', ' ').title()} (full setup)"


def _extract_fields(
    inline_item: dict[str, Any],
    component: dict[str, Any],
    gpio_occupancy: dict[int, str],
    component_id: str,
) -> dict[str, Any] | None:
    """
    Lift hardware-fixed fields out of an inline platform-list item.

    Pin / inverted fields are written as ``locked`` presets; other
    scalars come through as bare values (unlocked suggestions).
    Per-instance fields (``id``) are skipped — the dashboard generates
    its own ids and pre-filling the upstream value would just create
    rename friction or duplicate-id collisions.

    Returns ``None`` when the upstream item carries an unfillable
    placeholder (e.g. ``address: (FILL IN ONE-WIRE BUS ADDRESS)``).
    The caller drops the whole featured-component entry in that case
    rather than emit a preset that would compile but not run.
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
        if _is_placeholder_value(fval):
            return None
        preset = _coerce_field_preset(ce, fval, fkey, inline_item, gpio_occupancy, component_id)
        if preset is not None:
            out[fkey] = preset
    return out


def _coerce_field_preset(
    config_entry: dict[str, Any],
    raw_value: Any,
    field_name: str,
    inline_item: dict[str, Any],
    gpio_occupancy: dict[int, str],
    component_id: str,
) -> Any:
    """
    Convert one upstream field value into a preset, or ``None`` to skip it.

    Pin entries record GPIO occupancy and emit a ``locked`` preset.
    Cross-component id references are dropped (the user picks at add
    time). Other simple scalars come through as either locked presets
    (when the field name looks hardware-fixed) or bare suggestions.
    """
    ce_type = config_entry.get("type")
    if ce_type == "pin":
        normalized = _normalize_pin_value(raw_value)
        gpio = _gpio_number(normalized)
        if gpio is None:
            # Reference-style pins or lambdas — skip silently.
            return None
        label = _occupancy_label(inline_item, component_id)
        gpio_occupancy.setdefault(gpio, label)
        return {"value": normalized, "locked": True}
    if ce_type == "id":
        # Cross-component id refs are resolved in pass 2 by
        # ``_apply_id_references`` once every kept component has its
        # local id assigned — emitting them here would lock in the
        # raw upstream value before remapping.
        return None
    if not _is_simple_scalar(raw_value):
        return None
    if _looks_lockable(field_name):
        return {"value": raw_value, "locked": True}
    return raw_value


def _occupancy_label(inline_item: dict[str, Any], component_id: str) -> str:
    """
    Build a human-readable label for a GPIO's ``occupied_by`` field.

    Strips ``${friendly_name}``-style template variables that survive
    in upstream ``name:`` / ``id:`` fields and would otherwise leak
    raw substitution syntax into the manifest. Falls back to the
    catalog component id when nothing readable remains.
    """
    return _clean_entity_name(inline_item) or component_id


def _clean_entity_name(inline_item: dict[str, Any]) -> str:
    """
    Pick a readable entity name from an inline-yaml item.

    Returns the upstream ``name:`` / ``id:`` value with any
    ``${...}`` template substitutions removed and surrounding
    whitespace / separators trimmed. Returns an empty string when no
    readable label remains — callers fall back to a derived default.
    """
    for key in ("name", "id"):
        candidate = inline_item.get(key)
        if not isinstance(candidate, str):
            continue
        cleaned = _TEMPLATE_VAR_RE.sub("", candidate).strip(" -_")
        cleaned = re.sub(r"\s+", " ", cleaned)
        if cleaned:
            return cleaned
    return ""


def _is_placeholder_value(value: Any) -> bool:
    """Return True for upstream "user must fill this in" sentinel strings."""
    if not isinstance(value, str):
        return False
    return any(p.search(value) for p in _PLACEHOLDER_PATTERNS)


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


def _make_record(  # noqa: C901, PLR0911 — distinct skip reasons each get their own early exit
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

    featured, bundles, gpio_occupancy = _extract_featured_components(
        src.inline_yaml, components_index
    )
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
        # Reference upstream raw URLs directly so the wheel doesn't have
        # to ship hundreds of MB of mirrored device photos. The loader
        # (``_resolve_images``) passes ``http(s)://`` entries through
        # untouched.
        record["images"] = [
            f"{_DEVICES_REPO_RAW_BASE}/{_DEVICES_SUBDIR.as_posix()}/{src.folder_name}/{name}"
            for name in src.images
        ]

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
    if bundles:
        record["featured_bundles"] = bundles

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
    Write ``boards/<id>/manifest.yaml``.

    Skips with a warning when *target_dir* already holds a non-imported
    manifest (slug collision with a hand-curated board). Images are
    referenced as upstream raw URLs in the manifest itself (see
    ``_build_record``); any pre-existing local ``images/`` subdir from
    older syncs is removed so the wheel doesn't carry stale mirrors.
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
        shutil.rmtree(images_dir)

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
    """Join the slim component index with each per-id body, keyed by component id."""
    if not _COMPONENTS_INDEX_JSON.is_file():
        raise SystemExit(
            f"{_COMPONENTS_INDEX_JSON} not found — run script/sync_components.py first."
        )
    raw = json.loads(_COMPONENTS_INDEX_JSON.read_text(encoding="utf-8"))
    by_id: dict[str, dict[str, Any]] = {}
    for comp in raw.get("components", []):
        cid = comp.get("id")
        if not cid:
            continue
        body_path = _COMPONENTS_BODIES_DIR / f"{cid}.json"
        if body_path.is_file():
            body = json.loads(body_path.read_text(encoding="utf-8"))
            by_id[cid] = {**comp, **body}
        else:
            by_id[cid] = comp
    return by_id


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
