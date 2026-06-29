#!/usr/bin/env python3
"""
Generate ``definitions/boards/<id>/manifest.yaml`` from devices.esphome.io.

The upstream repo (https://github.com/esphome/devices.esphome.io) is a
Docusaurus site whose 760+ device pages each have YAML front matter
plus a ``yaml`` config, either inline or referenced from a sibling file
(``yaml file=config.yaml``). This script clones the repo, walks the
device pages, and emits one ``boards/<id>/manifest.yaml`` per device
that meets the strict acceptance bar (parseable yaml config, identifiable
board id, at least one local image, at least one extractable featured
component).

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
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from esphome_device_builder.constants import (  # noqa: E402
    BOARD_PIN_KEYS,
    DEVICE_IMPORT_SOURCE_TYPE,
)
from esphome_device_builder.helpers.pin_gpio import parse_board_gpio  # noqa: E402
from esphome_device_builder.models.boards import Esp32Variant  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LOGGER = logging.getLogger("sync_esphome_devices")

_DEFINITIONS_DIR = _REPO_ROOT / "esphome_device_builder" / "definitions"
_BOARDS_DIR = _DEFINITIONS_DIR / "boards"
_COMPONENTS_INDEX_JSON = _DEFINITIONS_DIR / "components.index.json"
_COMPONENTS_BODIES_DIR = _DEFINITIONS_DIR / "components"
_CACHE_ROOT = _REPO_ROOT / ".cache"
_DEVICES_CLONE_DIR = _CACHE_ROOT / "esphome-devices"
_DEVICES_REPO_URL = "https://github.com/esphome/devices.esphome.io.git"
_DEVICES_REPO_BRANCH = "main"
_DEVICES_SUBDIR = Path("src/docs/devices")
_DEVICES_PAGE_BASE = "https://devices.esphome.io/devices"
_DEVICES_REPO_BLOB_BASE = "https://github.com/esphome/devices.esphome.io/blob/main"
_DEVICES_REPO_RAW_BASE = "https://raw.githubusercontent.com/esphome/devices.esphome.io/main"

# Closed enums upstream enforces (mirror of
# devices.esphome.io/src/utils/validFrontmatter.ts).
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

# ESPHome esp32 board ids encode the chip variant (``esp32-p4-evboard``,
# ``esp32-c6-devkitc-1``); infer it when a page gives ``board:`` but no
# ``variant:``. Load-bearing for esp32p4 — it has no built-in radio, so the
# wrong (classic-esp32) variant would mislabel it wifi-capable and emit a
# ``wifi:`` block that fails validation (P4 wifi needs ``esp32_hosted``). Built
# from ``Esp32Variant`` longest-first so e.g. ``esp32s31`` isn't swallowed by
# ``esp32s3`` (nor ``esp32c61`` by ``esp32c6``).
_ESP32_VARIANT_SUFFIXES = sorted(
    (v.value.removeprefix("esp32") for v in Esp32Variant if v is not Esp32Variant.ESP32),
    key=len,
    reverse=True,
)
# Suffix must end the token (next char is ``-``, ``_``, or end) — a bare ``\b``
# wouldn't fire before ``_`` (underscore is a word char) so ``esp32_s3_zero`` would
# miss, and it keeps ``s3`` from matching inside ``s31``.
_ESP32_BOARD_VARIANT_RE = re.compile(
    r"esp32[-_]?(" + "|".join(_ESP32_VARIANT_SUFFIXES) + r")(?=[-_]|$)"
)

# Built-in radio defaults inferred from the SoC family / variant. ESP32
# variants differ on what's built in: classic + S3 + C3 + C5 + C6 +
# C61 carry both wifi + BLE; S2 has wifi only; H2 has BLE/Thread but
# no wifi; P4 has neither built in. Onboard ethernet is *not* inferred
# from the SoC here — it's mined from an explicit upstream ``ethernet:``
# block by ``_extract_ethernet`` (which adds the ``ethernet`` flag).
# Zigbee / matter still aren't mined; we have no reliable upstream signal.
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

# Generated catalog categories for ESPHome's buses (script/sync_components.py
# ``_CATEGORY_OVERRIDES``). Mapping-style buses (i2c/spi/uart/modbus) collapse
# to ``"bus"``; platform-style buses (one_wire/canbus) keep their domain name as
# the category because they are ``IS_PLATFORM_COMPONENT`` and have no top-level
# component, so the dep name itself equals the category.
_BUS_CATEGORIES: frozenset[str] = frozenset({"bus", "one_wire", "canbus"})

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

# Legacy flat ``clk_mode: GPIO17_OUT`` encodes the RMII clock pin in the
# mode string; this pulls the GPIO number out for the occupancy map.
_CLK_MODE_PIN_RE = re.compile(r"GPIO(\d+)_")

# Hardware fields of a top-level ``ethernet:`` block worth locking as a
# featured-component preset (the PHY/pinout). Network/runtime fields
# (``manual_ip``, ``domain``, ``use_address``, ``mac_address``,
# ``enable_on_boot``, ...) are user/site-specific and deliberately omitted.
# Every key here is a real ``ethernet`` config_entry, so the locked preset
# passes manifest validation.
_ETHERNET_HW_FIELDS: frozenset[str] = frozenset(
    {
        "type",
        "mdc_pin",
        "mdio_pin",
        "clk",
        "clk_mode",
        "clk_pin",
        "phy_addr",
        "power_pin",
        "mosi_pin",
        "miso_pin",
        "cs_pin",
        "interrupt_pin",
        "reset_pin",
        "clock_speed",
    }
)

# Pin-valued ethernet fields → the ``occupied_by`` role shown in the pin
# picker. ``clk`` is nested (``{pin, mode}``) and handled separately.
_ETHERNET_PIN_ROLES: dict[str, str] = {
    "mdc_pin": "Ethernet MDC",
    "mdio_pin": "Ethernet MDIO",
    "clk_pin": "Ethernet CLK",
    "power_pin": "Ethernet Power",
    "mosi_pin": "Ethernet MOSI",
    "miso_pin": "Ethernet MISO",
    "cs_pin": "Ethernet CS",
    "interrupt_pin": "Ethernet INT",
    "reset_pin": "Ethernet RESET",
}

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
    config_yaml: dict[str, Any] | None  # from the inline fence or a file= sibling
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
    Clone or update the devices.esphome.io repo. Returns its path or None.

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
    _LOGGER.info("Cloning devices.esphome.io (shallow) to %s", target)
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
        _LOGGER.error("Could not clone devices.esphome.io: %s", exc)
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
# Capture the fence info string (group 1) so a ``file=`` reference is
# honoured; group 2 is the inline body, which is empty for a file-backed
# fence (``\n?`` makes the body and its trailing newline optional).
_YAML_FENCE_RE = re.compile(r"```ya?ml([^\n]*)\n(.*?)\n?```", re.DOTALL)
_FENCE_FILE_RE = re.compile(r"\bfile=(\S+)")
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


def _resolve_fenced_yaml(info: str, inline: str, device_dir: Path) -> str | None:
    """
    YAML for a ``yaml`` fence; a ``file=`` ref reads the sibling, traversal-guarded.

    ``url=`` and absent refs return the inline body; a guarded or missing
    ``file=`` returns ``None``.
    """
    match = _FENCE_FILE_RE.search(info)
    if match is None:
        return inline
    ref = match.group(1).removeprefix("./")
    if "/" in ref or "\\" in ref or ".." in ref:
        # Same single-folder scope as image refs; warn so the resulting
        # drop is diagnosable instead of a silent "no config" skip.
        _LOGGER.warning("ignoring out-of-folder file= config ref %r (%s)", ref, device_dir.name)
        return None
    path = device_dir / ref
    try:
        return path.read_text(encoding="utf-8") if path.is_file() else None
    except OSError:
        return None


def _first_config_yaml(body: str, device_dir: Path) -> tuple[dict[str, Any], str] | None:
    """
    First parseable ``yaml`` config fence as ``(parsed, raw_text)``, following ``file=``.

    A device page often splits optional snippets across separate fences. The
    onboard ``ethernet:`` block (real hardware we lift) is sometimes a standalone
    fence after the base config, so fold just that one into the primary fence when
    the primary lacks it — the other example snippets (BLE, OTA, …) are left out.
    """
    primary: tuple[dict[str, Any], str] | None = None
    ethernet: Any = None
    ethernet_text: str | None = None
    for match in _YAML_FENCE_RE.finditer(body):
        text = _resolve_fenced_yaml(match.group(1), match.group(2), device_dir)
        if text is None:
            continue
        parsed = _safe_load_yaml(text)
        if not isinstance(parsed, dict):
            continue
        if primary is None:
            primary = parsed, text
        if ethernet is None and isinstance(parsed.get("ethernet"), dict):
            ethernet = parsed["ethernet"]
            ethernet_text = text
        if primary is not None and ethernet is not None:
            break  # nothing later can change the result — the common single-fence case
    if primary is None:
        return None
    parsed, text = primary
    if ethernet is not None and "ethernet" not in parsed:
        parsed = {**parsed, "ethernet": ethernet}
        # A ``file=``-referenced ethernet fence isn't in *body*, so fold its
        # resolved text into the returned raw_text or the source hash would miss
        # edits to it. An inline fence is already in *body* — leave it be.
        if ethernet_text is not None and ethernet_text not in body:
            text = f"{text}\n{ethernet_text}"
    return parsed, text


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
        config = _first_config_yaml(body, device_dir)
        # Fold a ``file=``-referenced config into the hash so a config-
        # only edit still changes the recorded source hash; an inline
        # config is already part of *text*.
        config_text = config[1] if config is not None else None
        hash_src = text if config_text is None or config_text in text else f"{text}\n{config_text}"
        yield _DeviceSource(
            folder_name=device_dir.name,
            page_path=page_path,
            frontmatter=frontmatter,
            body=body,
            content_hash=_hash_content(hash_src),
            config_yaml=config[0] if config is not None else None,
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
    if isinstance(raw, dict):
        return _gpio_number(raw.get("number"))
    return parse_board_gpio(raw)


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


def _expander_keys(pin_value: Any) -> set[str]:
    """Return provider keys in a long-form pin dict that reference an I/O-expander hub.

    A provider key is any key beyond the standard board-GPIO ``BOARD_PIN_KEYS``
    whose value is a hub instance id.
    """
    if not isinstance(pin_value, dict):
        return set()
    return {k for k in pin_value.keys() - BOARD_PIN_KEYS if isinstance(pin_value[k], str)}


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

    if soc == "esp32":
        if board and not variant:
            # Import-time resolver (connectivity is frozen here from the variant);
            # the catalog's authoritative ``_backfill_esp32_variants`` runs later
            # and only fixes ``esphome.variant``, not ``hardware.connectivity``.
            match = _ESP32_BOARD_VARIANT_RE.search(board.lower())
            variant = f"esp32{match.group(1)}" if match else None
        elif not board and variant:
            board = _ESP32_VARIANT_DEFAULT_BOARD.get(variant)

    return board, variant, framework


@dataclass
class _Candidate:
    """One inline-yaml item that survived filtering, ready to render."""

    item: dict[str, Any]
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


def _component_takes_name(component: dict[str, Any]) -> bool:
    """Whether the component's schema declares a top-level ``name`` config entry."""
    return any(ce.get("key") == "name" for ce in component.get("config_entries") or [])


def _entry_has_useful_preset(candidate: _Candidate, entry: dict[str, Any]) -> bool:
    """
    Return True when *entry* carries a real preset beyond the auto-injected ``id`` / ``name``.

    Lets ``_select_survivors`` distinguish skeleton components (no
    real fields) from consumers whose only contribution is a resolved
    ``output:`` reference — both have empty pass-1 ``fields`` but only
    the latter is worth keeping in the manifest.
    """
    fields = entry["fields"]
    auto_keys = {"id", "name"} if _component_takes_name(candidate.component) else {"id"}
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
    ``id`` and (for components whose schema takes one) ``name`` fields.
    """
    fields = dict(candidate.fields)
    _apply_id_references(fields, candidate.item, candidate.component, id_map)
    fields["id"] = candidate.local_id
    if _component_takes_name(candidate.component):
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


def _fold_requires_into_bundles(
    bundles: list[dict[str, Any]], featured: list[dict[str, Any]]
) -> None:
    """
    Prepend each bundle member's ``requires`` prerequisites to the bundle.

    Bundles are derived from id references before hubs are lifted, so a member's
    ``requires`` (bus then hub) isn't reflected yet. Folding them in — ahead of
    the members, deduped, order-preserving — makes a full-setup bundle a
    complete config and lets the synthesized ``all_recommended`` bundle collapse
    into it instead of shipping a near-duplicate.
    """
    by_id = {entry["id"]: entry for entry in featured}
    for bundle in bundles:
        members = bundle["component_ids"]
        seen = set(members)
        prereqs: list[str] = []
        for member in members:
            for req in by_id.get(member, {}).get("requires") or []:
                if req not in seen:
                    seen.add(req)
                    prereqs.append(req)
        if prereqs:
            bundle["component_ids"] = [*prereqs, *members]


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
    """Pick a human-readable bundle name from the consumer's upstream item.

    ESPHome's ``name: None`` sentinel (the entity adopts the device's friendly
    name) is kept as the entity's own name but is not a usable label, so the
    bundle falls back to the platform rather than read "None (full setup)".
    """
    cleaned = _clean_entity_name(cand.item)
    if cleaned and cleaned.casefold() != "none":
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


def _coerce_field_preset(  # noqa: PLR0911 — distinct field shapes each get their own early exit
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
        if isinstance(raw_value, list):
            return _coerce_pin_list(raw_value, inline_item, gpio_occupancy, component_id)
        normalized = _normalize_pin_value(raw_value)
        if _expander_keys(normalized):
            # Pin on an I/O expander: ``number`` is an expander channel, not a
            # board GPIO, so it occupies no board pin. The referenced hub is
            # materialized separately by ``_extract_expander_hubs``.
            return {"value": normalized, "locked": True}
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


def _coerce_pin_list(
    raw_value: list[Any],
    inline_item: dict[str, Any],
    gpio_occupancy: dict[int, str],
    component_id: str,
) -> dict[str, Any] | None:
    """
    Lock a list-valued pin field (octal SPI ``data_pins``, parallel buses).

    The whole list is hardware-fixed, so it locks as a list and records every
    GPIO it occupies; a scalar ``_gpio_number`` would reject the list outright.
    ``None`` for an empty list.
    """
    normalized = [_normalize_pin_value(value) for value in raw_value]
    if not normalized:
        return None
    label = _occupancy_label(inline_item, component_id)
    for value in normalized:
        gpio = _gpio_number(value)
        if gpio is not None:
            gpio_occupancy.setdefault(gpio, label)
    return {"value": normalized, "locked": True}


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


def _eth_value_safe(value: Any) -> bool:
    """Return True when *value* carries no ``${...}`` template or fill-in placeholder."""
    if isinstance(value, str):
        return "${" not in value and not _is_placeholder_value(value)
    if isinstance(value, dict):
        return all(_eth_value_safe(v) for v in value.values())
    if isinstance(value, list):
        return all(_eth_value_safe(v) for v in value)
    return True


def _extract_ethernet(config: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[int, str]]:
    """
    Mine a top-level ``ethernet:`` block into a locked featured component.

    Returns ``(featured_entry, gpio_occupancy)`` — the entry is ``None``
    when there's no ``ethernet:`` block, it lacks a PHY ``type``, or any
    hardware value is templated/placeholder (we never lock an unresolved
    ``${...}``). Only the PHY/pinout fields are lifted; the pin GPIOs are
    returned for the pin picker's occupancy map.

    This only runs for imported boards. A board where upstream lacks an
    ``ethernet:`` block, or whose ethernet must differ from upstream (a
    second hardware revision), needs a hand-curated, non-``source`` board
    the sync never overwrites — see ``gl_inet_gl_s10_v2``.
    """
    eth = config.get("ethernet")
    if not isinstance(eth, dict) or not isinstance(eth.get("type"), str):
        return None, {}
    fields: dict[str, Any] = {}
    occupancy: dict[int, str] = {}
    for key, value in eth.items():
        if key not in _ETHERNET_HW_FIELDS:
            continue
        if not _eth_value_safe(value):
            return None, {}
        if key in _ETHERNET_PIN_ROLES:
            # A pin field that doesn't resolve to a concrete GPIO (a
            # ``!secret`` tag, a lambda, ...) can't be locked into a valid
            # preset — distrust the whole block rather than emit garbage.
            gpio = _gpio_number(value)
            if gpio is None:
                return None, {}
            occupancy[gpio] = _ETHERNET_PIN_ROLES[key]
        fields[key] = {"value": value, "locked": True}
    clk = eth.get("clk")
    if isinstance(clk, dict):
        gpio = _gpio_number(clk.get("pin"))
        if gpio is None:
            return None, {}
        occupancy[gpio] = "Ethernet CLK"
    clk_mode = eth.get("clk_mode")
    if isinstance(clk_mode, str) and (m := _CLK_MODE_PIN_RE.match(clk_mode)):
        occupancy[int(m.group(1))] = "Ethernet CLK"
    entry = {
        "id": "onboard_ethernet",
        "component_id": "ethernet",
        "name": "Onboard Ethernet",
        "fields": fields,
    }
    return entry, occupancy


def _as_block_list(raw: Any) -> list[Any]:
    """Normalize a top-level component block (list, single mapping, or absent) to a list."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        return [raw]
    return []


def _block_mappings(raw: Any) -> list[dict[str, Any]]:
    """Top-level blocks of a component key, keeping only the mapping ones."""
    return [block for block in _as_block_list(raw) if isinstance(block, dict)]


def _find_hub_block(raw: Any, instance_id: str) -> dict[str, Any] | None:
    """
    Return the top-level hub block (``pcf8574:``) a pin's expander ref resolves to.

    Prefers an exact ``id`` match. Falls back to the sole block when the
    provider has exactly one and it carries no ``id`` — the source left the
    single hub's id implicit (ESPHome auto-generates one), so the pin's
    referenced id is adopted as the hub id at materialization. A mismatch
    against an explicitly-ided block, or an ambiguous multi-hub provider,
    yields ``None`` rather than guessing.
    """
    blocks = _block_mappings(raw)
    for block in blocks:
        if block.get("id") == instance_id:
            return block
    if len(blocks) == 1 and not blocks[0].get("id"):
        return blocks[0]
    return None


def _collect_expander_refs(
    featured: list[dict[str, Any]], components_index: dict[str, dict[str, Any]]
) -> tuple[list[tuple[dict[str, Any], list[tuple[str, str]]]], list[tuple[str, str]]]:
    """Find expander ``(hub_component_id, instance_id)`` refs in featured pin presets.

    Returns the consumers paired with their refs, plus the de-duplicated refs in
    first-seen order.
    """
    consumers: list[tuple[dict[str, Any], list[tuple[str, str]]]] = []
    ordered_refs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for entry in featured:
        refs: list[tuple[str, str]] = []
        for preset in entry.get("fields", {}).values():
            value = preset.get("value") if isinstance(preset, dict) else None
            for key in _expander_keys(value):
                if key not in components_index:
                    continue
                ref = (key, value[key])
                refs.append(ref)
                if ref not in seen:
                    seen.add(ref)
                    ordered_refs.append(ref)
        if refs:
            consumers.append((entry, refs))
    return consumers, ordered_refs


def _ensure_buses(
    hub_component: dict[str, Any],
    hub_block: dict[str, Any],
    config: dict[str, Any],
    components_index: dict[str, dict[str, Any]],
    used_ids: set[str],
    bus_local: dict[tuple[str, str | None], str],
    occupancy: dict[int, str],
    extra: list[dict[str, Any]],
) -> tuple[list[str], dict[str, str]]:
    """
    Materialize (once) the bus each hub dependency resolves to.

    Honors the hub's ``<bus>_id`` (e.g. ``i2c_id: bus_a``) so a board with more
    than one bus picks the right one; otherwise the sole bus. Returns the bus
    local ids (for ``requires``) and the ``{<bus>_id: source_id}`` references to
    lock onto the hub so it points at the bus that was lifted.
    """
    bus_ids: list[str] = []
    bus_refs: dict[str, str] = {}
    for dep in hub_component.get("dependencies") or []:
        ref_field = f"{dep}_id"
        instance = hub_block.get(ref_field)
        instance = instance if isinstance(instance, str) and instance else None
        key = (dep, instance)
        local = bus_local.get(key)
        if local is None:
            bus_entry, local, bus_occ = _materialize_bus(
                dep, instance, config, components_index, used_ids
            )
            if bus_entry is None:
                continue
            used_ids.add(local)
            bus_local[key] = local
            occupancy.update(bus_occ)
            extra.append(bus_entry)
        if local not in bus_ids:
            bus_ids.append(local)
        if instance is not None:
            bus_refs[ref_field] = instance
    return bus_ids, bus_refs


def _wire_consumer_requires(
    consumers: list[tuple[dict[str, Any], list[tuple[str, str | None]]]],
    hub_prereqs: dict[tuple[str, str | None], list[str]],
) -> None:
    """
    Merge each consumer's prerequisite chains into its ``requires``, deduped.

    Seeds from any existing ``requires`` so a later pass adds to an earlier pass's
    stamp rather than clobbering it (a leaf can need both a hub and a bus).
    """
    for entry, refs in consumers:
        requires: list[str] = list(entry.get("requires") or [])
        for ref in refs:
            for prereq in hub_prereqs.get(ref, []):
                if prereq not in requires:
                    requires.append(prereq)
        if requires:
            entry["requires"] = requires


def _drop_unresolved_consumers(
    featured: list[dict[str, Any]],
    consumers: list[tuple[dict[str, Any], list[tuple[str, str]]]],
    hub_prereqs: dict[tuple[str, str], list[str]],
) -> None:
    """
    Drop (in place) consumers whose hub couldn't be materialized.

    A skipped hub (placeholder / ambiguous / no id) leaves its consumer with a
    locked ``pin: {<provider>: <hub_id>, ...}`` preset pointing at a hub that
    never ships — a dangling reference that won't compile. Remove the consumer
    alongside its hub rather than emit the broken config this lift prevents.
    """
    dropped = {
        id(entry) for entry, refs in consumers if any(ref not in hub_prereqs for ref in refs)
    }
    if not dropped:
        return
    featured[:] = [entry for entry in featured if id(entry) not in dropped]
    consumers[:] = [(entry, refs) for entry, refs in consumers if id(entry) not in dropped]


def _unique_local_id(base: str, used: set[str], fallback: str) -> str:
    """Return *base* (or *fallback*) made unique against *used*."""
    candidate = base or fallback
    if candidate not in used:
        return candidate
    counter = 2
    while f"{candidate}_{counter}" in used:
        counter += 1
    return f"{candidate}_{counter}"


def _materialize_bus(
    bus_domain: str,
    instance_id: str | None,
    config: dict[str, Any],
    components_index: dict[str, dict[str, Any]],
    used_ids: set[str],
) -> tuple[dict[str, Any] | None, str, dict[int, str]]:
    """
    Lift the bus a consumer depends on into a featured entry, both bus shapes.

    Mapping-style buses (``i2c:`` / ``spi:`` / ``uart:`` / ``modbus:``) resolve to
    a top-level component and carry an explicit ``id`` we lock onto. Platform-style
    buses (``one_wire: - platform: gpio`` / ``canbus:``) resolve to a
    ``<domain>.<platform>`` component and may omit an ``id`` (a sole bus is
    auto-detected), so the id is optional there. *instance_id* names the specific
    bus when the consumer pins one (``i2c_id: bus_a``); otherwise the sole bus.
    Returns ``(None, "", {})`` when the bus is absent, ambiguous (no ``*_id`` and
    more than one bus), placeholder, or a mapping-style bus without an upstream
    ``id`` — the catalog component's own ``dependencies`` then surfaces the bare
    bus via the add-dialog's missing-dependency banner instead.
    """
    blocks = _block_mappings(config.get(bus_domain))
    if instance_id is not None:
        block = next((b for b in blocks if b.get("id") == instance_id), None)
    elif len(blocks) == 1:
        block = blocks[0]
    else:
        block = None
    if block is None:
        return None, "", {}
    component = components_index.get(bus_domain)
    if component is not None:
        component_id = bus_domain
        bus_inst = block.get("id")
        if not isinstance(bus_inst, str) or not bus_inst:
            return None, "", {}
    else:
        platform = block.get("platform")
        if not isinstance(platform, str) or not platform:
            return None, "", {}
        component_id = f"{bus_domain}.{platform}"
        component = components_index.get(component_id)
        if component is None or not _is_bus_category(component):
            return None, "", {}
        bus_inst = block.get("id")
        bus_inst = bus_inst if isinstance(bus_inst, str) and bus_inst else None
    occupancy: dict[int, str] = {}
    fields = _extract_fields(block, component, occupancy, component_id)
    if fields is None:
        return None, "", {}
    if bus_inst:
        fields["id"] = {"value": bus_inst, "locked": True}
    local_id = _unique_local_id(_sanitize_local_id(bus_inst or ""), used_ids, f"{bus_domain}_bus")
    return {"id": local_id, "component_id": component_id, "fields": fields}, local_id, occupancy


def _extract_expander_hubs(
    config: dict[str, Any],
    featured: list[dict[str, Any]],
    components_index: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[int, str]]:
    """
    Materialize the I/O-expander hubs (and their bus) referenced by featured pins.

    A gpio entity whose locked pin sits on an expander
    (``pin: {pcf8574: pcf8574_hub_in_1, ...}``) references a top-level hub block
    the platform-list extraction drops. This lifts each referenced hub — with its
    ``id`` locked to the upstream value so it matches the pin reference — plus the
    single i2c/spi bus it depends on, and stamps ``requires`` on every consumer
    (bus first, then hub) so the dashboard adds the prerequisites first.

    Mutates *featured*: adds ``requires`` to resolved consumers and drops
    consumers whose hub couldn't be materialized (so no dangling expander
    reference ships). Returns the new hub/bus entries to prepend and the GPIOs
    their bus pins occupy.
    """
    consumers, ordered_refs = _collect_expander_refs(featured, components_index)
    if not ordered_refs:
        return [], {}
    return _materialize_hubs(
        config,
        featured,
        components_index,
        consumers,
        ordered_refs,
        resolve_block=lambda cid, instance_id: _find_hub_block(config.get(cid), instance_id),
        driver=False,
    )


def _materialize_hubs(
    config: dict[str, Any],
    featured: list[dict[str, Any]],
    components_index: dict[str, dict[str, Any]],
    consumers: list[tuple[dict[str, Any], list[tuple[str, str | None]]]],
    ordered_refs: list[tuple[str, str | None]],
    *,
    resolve_block: Callable[[str, str | None], dict[str, Any] | None],
    driver: bool,
) -> tuple[list[dict[str, Any]], dict[int, str]]:
    """
    Lift each hub (and its bus) into a locked entry; stamp consumers' ``requires``.

    *resolve_block* locates a hub's top-level block. *driver* True skips a hub
    missing a required pin and keeps its consumer; False emits on any non-None
    lift and drops a consumer whose hub didn't materialize.
    """
    used_ids = {entry["id"] for entry in featured}
    extra: list[dict[str, Any]] = []
    occupancy: dict[int, str] = {}
    bus_local: dict[tuple[str, str | None], str] = {}
    # Each materialized hub keyed by its ref, carrying the local id + the bus
    # ids it needs — the ordered prerequisite chain a consumer references.
    hub_prereqs: dict[tuple[str, str | None], list[str]] = {}

    for hub_cid, instance_id in ordered_refs:
        hub_component = components_index.get(hub_cid)
        block = resolve_block(hub_cid, instance_id)
        if hub_component is None or block is None:
            continue
        # Record the hub's own pin occupancy locally and merge only on success:
        # a shift-register hub puts board GPIOs (data/clock/latch) here, and a
        # placeholder field part-way through the block must not leave those pins
        # marked occupied for a hub we then drop.
        hub_occ: dict[int, str] = {}
        fields = _extract_fields(block, hub_component, hub_occ, hub_cid)
        if fields is None or (driver and not fields):
            continue
        if driver and not _required_pin_keys(hub_component) <= fields.keys():
            # A required pin didn't parse (lambda / reference): skip the hub and
            # leave ``requires`` unstamped so the dep banner covers it, rather than
            # ship a pinless hub that compiles into an invalid config.
            continue
        occupancy.update(hub_occ)
        bus_ids, bus_refs = _ensure_buses(
            hub_component, block, config, components_index, used_ids, bus_local, occupancy, extra
        )
        base = _sanitize_local_id(instance_id) if instance_id else ""
        hub_id = _unique_local_id(base, used_ids, f"{hub_cid}{'_hub' if driver else ''}")
        used_ids.add(hub_id)
        # Lock the hub's id to the upstream value an expander pin ref points at;
        # a sole driver hub with no upstream id keeps its generated local id.
        if instance_id:
            fields["id"] = {"value": instance_id, "locked": True}
        # Lock the hub onto the bus it was lifted from (multi-bus boards), so it
        # doesn't fall back to esphome's default i2c pins.
        for ref_field, bus_inst in bus_refs.items():
            fields[ref_field] = {"value": bus_inst, "locked": True}
        hub_entry: dict[str, Any] = {"id": hub_id, "component_id": hub_cid, "fields": fields}
        if bus_ids:
            hub_entry["requires"] = bus_ids
        extra.append(hub_entry)
        hub_prereqs[(hub_cid, instance_id)] = [*bus_ids, hub_id]

    if not driver:
        _drop_unresolved_consumers(featured, consumers, hub_prereqs)
    _wire_consumer_requires(consumers, hub_prereqs)
    return extra, occupancy


def _required_pin_keys(component: dict[str, Any]) -> set[str]:
    """Keys of the component's required ``type: "pin"`` config entries."""
    return {
        ce["key"]
        for ce in component.get("config_entries") or []
        if ce.get("type") == "pin" and ce.get("required") and isinstance(ce.get("key"), str)
    }


def _is_driver_hub(component: dict[str, Any]) -> bool:
    """
    Return True for a hub a consumer binds by catalog dependency that owns board pins.

    LED-driver hubs (``bp5758d``, ``sm2135``, ...) sit as a top-level block with
    their own ``clock_pin`` / ``data_pin`` and are pulled in by an
    ``output.<driver>`` platform's ``dependencies`` rather than by a pin
    reference. Buses (``i2c`` / ``spi`` / ``uart``) are excluded — a hub's own
    bus is materialized through ``_ensure_buses`` instead.
    """
    if component.get("category") == "bus":
        return False
    return any(ce.get("type") == "pin" for ce in component.get("config_entries") or [])


def _sole_hub_block(raw: Any) -> dict[str, Any] | None:
    """Return the single top-level hub mapping, or ``None`` if absent/ambiguous."""
    blocks = _block_mappings(raw)
    return blocks[0] if len(blocks) == 1 else None


def _collect_driver_hub_refs(
    config: dict[str, Any],
    featured: list[dict[str, Any]],
    components_index: dict[str, dict[str, Any]],
) -> tuple[
    list[tuple[dict[str, Any], list[tuple[str, str | None]]]],
    list[tuple[str, str | None]],
]:
    """Find driver-hub ``(hub_component_id, instance_id)`` refs via catalog dependencies.

    Returns the consumers paired with their refs, plus the de-duplicated refs in
    first-seen order. A hub already present as a featured entry (e.g. lifted by
    the expander path) is skipped so it isn't materialized twice.
    """
    existing_cids = {entry["component_id"] for entry in featured}
    consumers: list[tuple[dict[str, Any], list[tuple[str, str | None]]]] = []
    for entry in featured:
        component = components_index.get(entry["component_id"])
        if component is None:
            continue
        refs: list[tuple[str, str | None]] = []
        for dep in component.get("dependencies") or []:
            if not isinstance(dep, str) or dep in existing_cids:
                continue
            hub = components_index.get(dep)
            if hub is None or not _is_driver_hub(hub):
                continue
            block = _sole_hub_block(config.get(dep))
            if block is None:
                continue
            instance_id = block.get("id") if isinstance(block.get("id"), str) else None
            refs.append((dep, instance_id))
        if refs:
            consumers.append((entry, refs))
    # First-seen-order dedup across all consumers — the shared spine consumes one
    # ordered ref list.
    ordered_refs = list(dict.fromkeys(ref for _, refs in consumers for ref in refs))
    return consumers, ordered_refs


def _extract_driver_hubs(
    config: dict[str, Any],
    featured: list[dict[str, Any]],
    components_index: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[int, str]]:
    """
    Materialize output-driver hubs bound through the catalog dependency graph.

    Unlike an I/O-expander hub (referenced by a consumer's pin preset), an
    LED-driver hub (``bp5758d:``) is pulled in by an ``output.<driver>``
    platform's ``dependencies`` and owns its own board pins. The platform-list
    extraction drops the top-level block, leaving the user with empty
    ``clock_pin`` / ``data_pin`` dropdowns. This lifts each such hub as a locked
    featured entry (plus any bus it depends on) and stamps ``requires`` on the
    consumers so the dashboard adds the hub first with its pins pre-filled.

    Mutates *featured*: adds ``requires`` to resolved consumers. The consumers
    stay valid without their hub (the add-dialog's missing-dependency banner
    still covers an unmaterialized one), so — unlike the expander path — none
    are dropped.
    """
    consumers, ordered_refs = _collect_driver_hub_refs(config, featured, components_index)
    if not ordered_refs:
        return [], {}
    return _materialize_hubs(
        config,
        featured,
        components_index,
        consumers,
        ordered_refs,
        resolve_block=lambda cid, _instance_id: _sole_hub_block(config.get(cid)),
        driver=True,
    )


def _find_consumer_block(config: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any] | None:
    """
    Re-find a featured leaf's upstream source block to recover its ``<bus>_id`` ref.

    The finalized entry drops ``type: "id"`` cross-references (``spi_id``), so the
    bus a consumer binds is read back from the source. Prefers the sole block of
    the entry's platform; else the block whose sanitized ``id`` equals the entry's
    local id. ``None`` when ambiguous — the bus then falls back to the sole/none
    resolution in ``_materialize_bus``.
    """
    domain, _, platform = entry["component_id"].partition(".")
    blocks = [b for b in _block_mappings(config.get(domain)) if b.get("platform") == platform]
    if len(blocks) == 1:
        return blocks[0]
    for block in blocks:
        if _sanitize_local_id(str(block.get("id") or "")) == entry["id"]:
            return block
    return None


def _is_bus_category(component: dict[str, Any]) -> bool:
    """Whether a resolved catalog component is a bus (mapping- or platform-style)."""
    return component.get("category") in _BUS_CATEGORIES


def _is_bus_dep(dep: str, components_index: dict[str, dict[str, Any]]) -> bool:
    """
    Whether *dep* names one of ESPHome's buses, mapping- or platform-style.

    Mapping-style buses (i2c/spi/uart/modbus) resolve to a top-level component
    whose category is ``"bus"``. Platform-style buses (one_wire/canbus) have no
    top-level component; their schema lives under ``<dep>.<platform>`` and the
    dep name itself equals the bus category, so it is matched against the set.
    """
    component = components_index.get(dep)
    if component is not None:
        return _is_bus_category(component)
    return dep in _BUS_CATEGORIES


def _collect_bus_dep_refs(
    config: dict[str, Any],
    featured: list[dict[str, Any]],
    components_index: dict[str, dict[str, Any]],
) -> tuple[
    list[tuple[dict[str, Any], list[tuple[str, str | None]]]],
    list[tuple[str, str | None]],
]:
    """Find the buses featured leaves depend on directly via catalog dependencies.

    Returns the consumers paired with their ``(bus_component_id, instance_id)``
    refs, plus the de-duplicated refs in first-seen order. A bus already present
    as a featured entry (lifted by an earlier pass, or featured in its own right)
    is skipped via ``existing_cids`` so it isn't materialized twice.
    """
    existing_cids = {entry["component_id"] for entry in featured}
    consumers: list[tuple[dict[str, Any], list[tuple[str, str | None]]]] = []
    for entry in featured:
        # Infra entries lifted by an earlier pass (bus / hub / ethernet) have a
        # bare component id; only platform leaves (``<domain>.<platform>``) bind a
        # bus by catalog dependency.
        if "." not in entry["component_id"]:
            continue
        component = components_index.get(entry["component_id"])
        if component is None:
            continue
        block = _find_consumer_block(config, entry)
        refs: list[tuple[str, str | None]] = []
        for dep in component.get("dependencies") or []:
            if not isinstance(dep, str) or dep in existing_cids:
                continue
            if not _is_bus_dep(dep, components_index):
                continue
            instance = block.get(f"{dep}_id") if block else None
            instance = instance if isinstance(instance, str) and instance else None
            refs.append((dep, instance))
        if refs:
            consumers.append((entry, refs))
    ordered_refs = list(dict.fromkeys(ref for _, refs in consumers for ref in refs))
    return consumers, ordered_refs


def _extract_bus_deps(
    config: dict[str, Any],
    featured: list[dict[str, Any]],
    components_index: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[int, str]]:
    """
    Lift each bus a featured leaf depends on directly, both bus shapes.

    Covers mapping-style (display→spi, sensor→i2c) and platform-style
    (dallas_temp→one_wire) buses. The platform-list extraction drops the top-level
    ``spi:`` / ``one_wire:`` block, so a leaf binding a bus by catalog dependency
    lands with empty bus pins and an unsatisfied ``requires component <bus>``. This
    lifts each such bus as a locked entry (pins pre-filled) and stamps ``requires``
    on the consumers so the dashboard adds the bus first; a bus shared by several
    leaves lifts once.

    Mutates *featured*: merges (does not overwrite) ``requires`` on resolved
    consumers, so a leaf that also needs a hub keeps both. An unresolved bus
    (absent / ambiguous) leaves ``requires`` unstamped — the add-dialog's
    missing-dependency banner still covers it; a bus already lifted by an earlier
    pass and depended on by a different leaf is not re-stamped (matches
    ``_collect_driver_hub_refs``).
    """
    consumers, ordered_refs = _collect_bus_dep_refs(config, featured, components_index)
    if not ordered_refs:
        return [], {}
    used_ids = {entry["id"] for entry in featured}
    extra: list[dict[str, Any]] = []
    occupancy: dict[int, str] = {}
    bus_local: dict[tuple[str, str | None], str] = {}
    for dep, instance in ordered_refs:
        bus_entry, local, bus_occ = _materialize_bus(
            dep, instance, config, components_index, used_ids
        )
        if bus_entry is None:
            continue
        used_ids.add(local)
        bus_local[(dep, instance)] = local
        occupancy.update(bus_occ)
        extra.append(bus_entry)
    _wire_consumer_requires(consumers, {ref: [local] for ref, local in bus_local.items()})
    return extra, occupancy


def _make_record(  # noqa: C901, PLR0911, PLR0912 — distinct skip reasons each get their own early exit
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
    if src.config_yaml is None:
        return None, "no parseable yaml config"
    if not src.images:
        return None, "no local images"
    # ``type:`` is optional — only used downstream for tag inference.
    # A page without it is still importable.
    type_field = fm.get("type") if isinstance(fm.get("type"), str) else None

    soc, soc_block = _resolve_soc(fm, src.config_yaml)
    if soc is None:
        return None, "soc family not in upstream enum"
    board, variant, framework = _resolve_board_and_variant(soc, soc_block)
    if not board:
        return None, f"no concrete board id for {soc}"

    featured, bundles, gpio_occupancy = _extract_featured_components(
        src.config_yaml, components_index
    )
    # Lift a top-level ``ethernet:`` block (the upstream importer otherwise
    # drops it) so wired boards get their onboard-network provider — and so
    # an ethernet-only page isn't rejected by the no-featured gate below.
    eth_entry, eth_occupancy = _extract_ethernet(src.config_yaml)
    if eth_entry is not None:
        featured = [eth_entry, *featured]
        gpio_occupancy = {**eth_occupancy, **gpio_occupancy}
    # Lift the prerequisites a featured leaf needs but the platform-list
    # extraction drops, each stamping ``requires`` so the dashboard adds them
    # first with pins pre-filled: I/O-expander hubs referenced by a featured
    # pin (else a dangling ``pcf8574: <id>``), output-driver hubs (bp5758d, ...)
    # bound by an output platform's catalog dependency, and the bus (spi/i2c/
    # uart) a display/sensor depends on directly. Order matters — each pass
    # dedups against the entries already lifted.
    for _lift in (_extract_expander_hubs, _extract_driver_hubs, _extract_bus_deps):
        lift_entries, lift_occupancy = _lift(src.config_yaml, featured, components_index)
        if lift_entries:
            featured = [*lift_entries, *featured]
            gpio_occupancy = {**lift_occupancy, **gpio_occupancy}
    # The per-consumer bundle is built from id references before hubs are
    # lifted, so fold each member's ``requires`` (bus/hub) back in — otherwise a
    # "full setup" lands the light + outputs without the driver hub they need.
    _fold_requires_into_bundles(bundles, featured)
    if not featured:
        return None, "no extractable featured components"

    record: dict[str, Any] = {
        "id": _slugify(src.folder_name),
        "name": title.strip(),
        "description": "Imported from devices.esphome.io — see linked docs for community notes.",
        "esphome": _build_esphome_block(soc, board, variant, framework),
    }

    connectivity = list(_connectivity_for(soc, variant) or [])
    if eth_entry is not None and "ethernet" not in connectivity:
        connectivity.append("ethernet")
    if connectivity:
        record["hardware"] = {"connectivity": connectivity}

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
        "type": DEVICE_IMPORT_SOURCE_TYPE,
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
    manifest_path = target_dir / "manifest.yaml"
    prior = _read_manifest_dict(manifest_path)
    # An existing manifest the sync doesn't own is a slug collision —
    # leave it untouched. Hand-curated boards (no ``source.type``) and
    # unparsable files both read as "not imported"; an unreadable file
    # ``prior is None`` so guard on the file existing, not on the parse.
    if manifest_path.is_file() and not _imported_remote_id(prior)[0]:
        _LOGGER.warning(
            "Skipping %s — slug collides with a hand-curated board (no source.type)",
            record["id"],
        )
        return None
    target_dir.mkdir(parents=True, exist_ok=True)

    # Carry a hand-curated ``full_config`` opt-out/opt-in across re-imports —
    # the importer never sets it (imports derive ``full_config`` from
    # ``source.type``), so an override only survives if preserved here. Re-insert
    # it right after ``esphome`` to keep manifest key order stable.
    prior_full_config = prior.get("full_config") if prior is not None else None
    if isinstance(prior_full_config, bool):
        rebuilt: dict[str, Any] = {}
        for key, value in record.items():
            rebuilt[key] = value
            if key == "esphome":
                rebuilt["full_config"] = prior_full_config
        record = rebuilt

    images_dir = target_dir / "images"
    if images_dir.is_dir():
        shutil.rmtree(images_dir)

    manifest_path.write_text(_dump_manifest(record), encoding="utf-8")
    return target_dir


def _read_manifest_dict(manifest_path: Path) -> dict[str, Any] | None:
    """Parse an existing ``manifest.yaml`` to a dict, or ``None`` if missing/unreadable."""
    if not manifest_path.is_file():
        return None
    try:
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def _imported_remote_id(prior: dict[str, Any] | None) -> tuple[bool, str | None]:
    """Return ``(is_imported, remote_id)`` for an already-parsed manifest dict."""
    source = prior.get("source") if prior is not None else None
    if not isinstance(source, dict) or source.get("type") != DEVICE_IMPORT_SOURCE_TYPE:
        return False, None
    remote_id = source.get("remote_id")
    return True, remote_id if isinstance(remote_id, str) else None


def _is_imported_manifest(manifest_path: Path) -> tuple[bool, str | None]:
    """Return ``(is_imported, remote_id)`` for an existing board manifest."""
    return _imported_remote_id(_read_manifest_dict(manifest_path))


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
