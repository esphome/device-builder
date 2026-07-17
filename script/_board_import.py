"""Shared helpers for board-catalog import sources (manifest emit/prune, YAML, ethernet)."""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from esphome_device_builder.helpers.pin_gpio import parse_board_gpio  # noqa: E402
from script._component_catalog import load_component_catalog  # noqa: E402
from script._manifest import ManifestError, load_manifest_dict  # noqa: E402

_LOGGER = logging.getLogger(__name__)

_DEFINITIONS_DIR = _REPO_ROOT / "esphome_device_builder" / "definitions"
_COMPONENTS_INDEX_JSON = _DEFINITIONS_DIR / "components.index.json"
_COMPONENTS_BODIES_DIR = _DEFINITIONS_DIR / "components"

# Map ESP32 chip variants to a sensible default PlatformIO board id —
# used when an upstream page declares ``esp32: { variant: esp32c3 }``
# without an explicit ``board:``. Picked to match what ESPHome itself
# defaults to for each variant.
ESP32_VARIANT_DEFAULT_BOARD: dict[str, str] = {
    "esp32": "esp32dev",
    "esp32s2": "esp32-s2-saola-1",
    "esp32s3": "esp32-s3-devkitc-1",
    "esp32c2": "esp32-c2-devkitm-1",
    "esp32c3": "esp32-c3-devkitm-1",
    "esp32c5": "esp32-c5-devkitc-1",
    "esp32c6": "esp32-c6-devkitc-1",
    "esp32c61": "esp32-c61-devkitc1",
    "esp32h2": "esp32-h2-devkitm-1",
    # Pre-rev3 board on purpose: ES firmware boots on both silicon revisions,
    # rev3-min firmware faults at boot on pre-rev3 chips.
    "esp32p4": "esp32-p4-evboard",
}

# Built-in radio defaults inferred from the SoC family / variant. ESP32
# variants differ on what's built in: classic + S3 + C3 + C5 + C6 +
# C61 carry both wifi + BLE; S2 has wifi only; H2 has BLE/Thread but
# no wifi; P4 has neither built in. Onboard ethernet is *not* inferred
# from the SoC here — it's mined from an explicit upstream ``ethernet:``
# block by ``extract_ethernet`` (which adds the ``ethernet`` flag).
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

# Deprecated flat ``clk_mode: GPIO<n>_(IN|OUT)`` encodes the RMII clock
# pin and direction in the mode string; folded into nested ``clk``
# ``{mode, pin}`` (upstream removal 2026.9.0).
_CLK_MODE_RE = re.compile(r"GPIO(\d+)_(IN|OUT)")

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


def safe_load_yaml(text: str) -> Any:
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


def dump_manifest(data: dict[str, Any]) -> str:
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
# Upstream metadata
# ---------------------------------------------------------------------------


def get_repo_revision(repo: Path) -> str:
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


def hash_content(text: str) -> str:
    """Return the SHA-256 hex digest of *text*."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Record building blocks
# ---------------------------------------------------------------------------


def is_placeholder_value(value: Any) -> bool:
    """Return True for upstream "user must fill this in" sentinel strings."""
    if not isinstance(value, str):
        return False
    return any(p.search(value) for p in _PLACEHOLDER_PATTERNS)


def gpio_number(raw: Any) -> int | None:
    """Extract a GPIO integer from any supported ESPHome pin shorthand."""
    if isinstance(raw, dict):
        return gpio_number(raw.get("number"))
    return parse_board_gpio(raw)


def build_pins(gpio_occupancy: dict[int, str]) -> list[dict[str, Any]]:
    """Synthesize one minimal pin entry per GPIO referenced by featured components."""
    return [
        {"gpio": gpio, "available": False, "occupied_by": gpio_occupancy[gpio]}
        for gpio in sorted(gpio_occupancy)
    ]


def build_esphome_block(
    soc: str,
    board: str,
    variant: str | None,
    framework: str | None,
    logger_hardware_uart: str | None = None,
) -> dict[str, Any]:
    """Compose the manifest's ``esphome:`` block, omitting empty optional fields."""
    out: dict[str, Any] = {"platform": soc, "board": board}
    if variant:
        out["variant"] = variant
    if framework in ("arduino", "esp-idf"):
        out["framework"] = framework
    if logger_hardware_uart:
        out["logger_hardware_uart"] = logger_hardware_uart
    return out


def connectivity_for(soc: str, variant: str | None) -> list[str] | None:
    """Return the built-in radio mix for *soc*/*variant*, or ``None`` for none."""
    if soc == "esp32":
        # Variants without an explicit override fall through to the
        # classic esp32 default (wifi + bluetooth).
        return _ESP32_VARIANT_CONNECTIVITY.get(variant or "esp32", ["wifi", "bluetooth"])
    return _SOC_CONNECTIVITY.get(soc)


def _eth_value_safe(value: Any) -> bool:
    """Return True when *value* carries no ``${...}`` template or fill-in placeholder."""
    if isinstance(value, str):
        return "${" not in value and not is_placeholder_value(value)
    if isinstance(value, dict):
        return all(_eth_value_safe(v) for v in value.values())
    if isinstance(value, list):
        return all(_eth_value_safe(v) for v in value)
    return True


def _normalized_clk(eth: dict[str, Any]) -> dict[str, Any] | None:
    """Fold deprecated flat ``clk_mode`` into nested ``clk``; ``None`` when unmappable."""
    if "clk_mode" not in eth:
        return eth
    # clk_mode exists only in upstream's RMII schema (SPI PHYs use clk_pin);
    # a block carrying it without the RMII-required mdc_pin is invalid upstream.
    if "mdc_pin" not in eth:
        return None
    without = {k: v for k, v in eth.items() if k != "clk_mode"}
    if "clk" in eth:
        return without
    clk_mode = eth["clk_mode"]
    # Upstream validates with cv.enum(upper=True, space="_"); mirror it.
    normalized = clk_mode.strip().upper().replace(" ", "_") if isinstance(clk_mode, str) else ""
    match = _CLK_MODE_RE.fullmatch(normalized)
    if match is None:
        return None
    mode = "CLK_EXT_IN" if match.group(2) == "IN" else "CLK_OUT"
    return {**without, "clk": {"pin": f"GPIO{match.group(1)}", "mode": mode}}


def extract_ethernet(config: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[int, str]]:
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
    eth = _normalized_clk(eth)
    if eth is None:
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
            gpio = gpio_number(value)
            if gpio is None:
                return None, {}
            occupancy[gpio] = _ETHERNET_PIN_ROLES[key]
        fields[key] = {"value": value, "locked": True}
    clk = eth.get("clk")
    if isinstance(clk, dict):
        gpio = gpio_number(clk.get("pin"))
        if gpio is None:
            return None, {}
        occupancy[gpio] = "Ethernet CLK"
    entry = {
        "id": "onboard_ethernet",
        "component_id": "ethernet",
        "name": "Onboard Ethernet",
        "fields": fields,
    }
    return entry, occupancy


# ---------------------------------------------------------------------------
# Emit + prune
# ---------------------------------------------------------------------------


def read_manifest_dict(manifest_path: Path) -> dict[str, Any] | None:
    """Parse an existing ``manifest.yaml`` to a dict, or ``None`` if missing/unreadable."""
    if not manifest_path.is_file():
        return None
    try:
        return load_manifest_dict(manifest_path)
    except (OSError, ManifestError) as exc:
        # A present-but-corrupt/unreadable manifest still returns None (the prior
        # state is unrecoverable either way), but log it so it isn't silently
        # indistinguishable from a manifest that never existed.
        _LOGGER.warning("Ignoring unreadable manifest %s: %s", manifest_path, exc)
        return None


def imported_remote_id(
    prior: dict[str, Any] | None, *, source_type: str
) -> tuple[bool, str | None]:
    """Return ``(is_imported, remote_id)`` for an already-parsed manifest dict."""
    source = prior.get("source") if prior is not None else None
    if not isinstance(source, dict) or source.get("type") != source_type:
        return False, None
    remote_id = source.get("remote_id")
    return True, remote_id if isinstance(remote_id, str) else None


def is_imported_manifest(manifest_path: Path, *, source_type: str) -> tuple[bool, str | None]:
    """Return ``(is_imported, remote_id)`` for an existing board manifest."""
    return imported_remote_id(read_manifest_dict(manifest_path), source_type=source_type)


def emit_manifest(record: dict[str, Any], *, source_type: str, boards_dir: Path) -> Path | None:
    """
    Write ``boards/<id>/manifest.yaml``.

    Skips with a warning when the target already holds a manifest this
    source doesn't own — a hand-curated board (no ``source.type``) or
    another import source's output. Images are referenced as upstream
    URLs in the manifest itself; any pre-existing local ``images/``
    subdir from older syncs is removed so the wheel doesn't carry stale
    mirrors.
    """
    target_dir = boards_dir / record["id"]
    manifest_path = target_dir / "manifest.yaml"
    prior = read_manifest_dict(manifest_path)
    # An existing manifest the sync doesn't own is a slug collision —
    # leave it untouched. Hand-curated boards (no ``source.type``) and
    # unparsable files both read as "not imported"; an unreadable file
    # ``prior is None`` so guard on the file existing, not on the parse.
    if manifest_path.is_file() and not imported_remote_id(prior, source_type=source_type)[0]:
        _LOGGER.warning(
            "Skipping %s — slug collides with a board this source doesn't own",
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

    manifest_path.write_text(dump_manifest(record), encoding="utf-8")
    return target_dir


def prune_removed(active_remote_ids: set[str], *, source_type: str, boards_dir: Path) -> list[str]:
    """Delete boards/<id>/ for any manifest this source owns that's no longer upstream."""
    removed: list[str] = []
    if not boards_dir.is_dir():
        return removed
    for child in sorted(boards_dir.iterdir()):
        if not child.is_dir():
            continue
        manifest = child / "manifest.yaml"
        if not manifest.is_file():
            continue
        is_imported, remote_id = is_imported_manifest(manifest, source_type=source_type)
        if not is_imported:
            continue
        if remote_id and remote_id in active_remote_ids:
            continue
        shutil.rmtree(child)
        removed.append(child.name)
    return removed


def load_components_index() -> dict[str, dict[str, Any]]:
    """Join the slim component index with each per-id body, keyed by component id."""
    if not _COMPONENTS_INDEX_JSON.is_file():
        raise SystemExit(
            f"{_COMPONENTS_INDEX_JSON} not found — run script/sync_components.py first."
        )
    return load_component_catalog(_COMPONENTS_INDEX_JSON, _COMPONENTS_BODIES_DIR)
