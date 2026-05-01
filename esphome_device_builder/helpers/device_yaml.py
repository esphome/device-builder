"""
Pure-function helpers for generating, parsing, and reading device YAML.

These utilities are intentionally state-free so they can be reused by
the devices controller, the device builder, and any future tool that
needs to inspect or synthesise an ESPHome config without instantiating
a controller.
"""

from __future__ import annotations

import base64
import re
import secrets
from pathlib import Path
from typing import TYPE_CHECKING

from esphome import const
from esphome.storage_json import StorageJSON, ext_storage_path

from ..models import Device

if TYPE_CHECKING:
    from ..models import BoardCatalogEntry

_PLATFORM_KEYS = frozenset({"esp32", "esp8266", "rp2040", "bk72xx", "rtl87xx", "ln882x", "nrf52"})

# Mirrors esphome's substitution regex (`config_validation.VARIABLE_PROG`):
# matches ``$name`` or ``${name}`` where name is alphanumeric + underscore.
_SUBSTITUTION_RE = re.compile(r"\$(\{[a-zA-Z0-9_]*\}|[a-zA-Z0-9_]+)")


# ---------------------------------------------------------------------------
# YAML generation
# ---------------------------------------------------------------------------


def generate_device_yaml(
    name: str,
    friendly_name: str,
    board: BoardCatalogEntry,
    ssid: str,
    psk: str,
) -> str:
    """
    Generate a complete device YAML config from a board definition.

    Produces the base config with platform settings, logging, API, OTA,
    and Wi-Fi — the most common/sane defaults for a new device.
    """
    esphome_cfg = board.esphome
    lines: list[str] = []

    # Board reference comment so users can find the source manifest
    board_label = board.name
    if board.manufacturer:
        board_label = f"{board.name} ({board.manufacturer})"
    lines.append(f"# Board: {board_label}")
    lines.append(f"# Definition: definitions/boards/{board.id}/manifest.yaml")
    lines.append("")

    # ESPHome core
    lines.append("esphome:")
    lines.append(f"  name: {name}")
    lines.append(f"  friendly_name: {friendly_name}")
    lines.append("")

    # Platform config
    # ESP32: variant + flash_size, board optional
    # All others: board is REQUIRED, no variant/flash_size
    platform = str(esphome_cfg.platform)
    hardware = board.hardware
    lines.append(f"{platform}:")

    if platform == "esp32":
        # ESP32 uses variant instead of board
        if esphome_cfg.variant:
            lines.append(f"  variant: {esphome_cfg.variant}")
        if hardware.flash_size:
            lines.append(f"  flash_size: {hardware.flash_size}")
        if esphome_cfg.framework:
            lines.append("  framework:")
            lines.append(f"    type: {esphome_cfg.framework}")
    else:
        # esp8266, rp2040, bk72xx, rtl87xx, ln882x, nrf52 — board is required
        lines.append(f"  board: {esphome_cfg.board}")

    lines.append("")

    # Logging
    lines.append("logger:")
    lines.append("")

    # Home Assistant API — unique encryption key per device
    api_key = base64.b64encode(secrets.token_bytes(32)).decode()
    lines.append("api:")
    lines.append("  encryption:")
    lines.append(f'    key: "{api_key}"')
    lines.append("")

    # OTA
    lines.append("ota:")
    lines.append("  - platform: esphome")
    lines.append("")

    # Wi-Fi (only for boards that support it)
    connectivity = [c.value for c in board.hardware.connectivity] if board.hardware else []
    has_wifi = "wifi" in connectivity or not connectivity
    if has_wifi:
        lines.append("wifi:")
        if ssid:
            lines.append(f"  ssid: {ssid}")
            lines.append(f"  password: {psk}")
        else:
            lines.append("  ssid: !secret wifi_ssid")
            lines.append("  password: !secret wifi_password")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# YAML parsing
# ---------------------------------------------------------------------------


def parse_platform_from_yaml(yaml_content: str) -> tuple[str, str, str]:
    """
    Extract ``(platform, pio_board, variant)`` from device YAML content.

    Looks at top-level platform keys (``esp32:``, ``esp8266:``, …) and
    reads the ``board:`` and ``variant:`` fields nested under them.
    Returns empty strings for fields that aren't present.
    """
    platform = ""
    pio_board = ""
    variant = ""
    in_platform = False

    for line in yaml_content.splitlines():
        if line and not line[0].isspace() and ":" in line:
            key = line.split(":")[0].strip()
            if key in _PLATFORM_KEYS:
                platform = key
                in_platform = True
            else:
                in_platform = False
            continue
        if not in_platform:
            continue
        stripped = line.strip()
        if stripped.startswith("board:"):
            pio_board = stripped.split(":", 1)[1].strip().strip('"').strip("'")
        elif stripped.startswith("variant:"):
            variant = stripped.split(":", 1)[1].strip().strip('"').strip("'")

    return platform, pio_board, variant


def detect_platform_from_yaml(path: Path) -> str:
    """
    Quick scan of a YAML file to find its platform key.

    Returns the empty string when the file is unreadable or contains no
    top-level platform key.
    """
    try:
        platform, _, _ = parse_platform_from_yaml(path.read_text(encoding="utf-8"))
        return platform
    except Exception:
        return ""


def device_uses_mqtt(yaml_content: str) -> bool:
    """
    Return True when the device YAML declares a top-level ``mqtt:`` block.

    The check is line-based so it handles invalid drafts and partially
    edited configs gracefully — no full YAML parse required.
    """
    for line in yaml_content.splitlines():
        if not line or line[0].isspace():
            continue
        stripped = line.strip()
        if stripped.startswith("#") or ":" not in stripped:
            continue
        if stripped.split(":", 1)[0].strip() == "mqtt":
            return True
    return False


def parse_esphome_meta(
    yaml_content: str,
) -> tuple[str | None, str | None, str | None]:
    """
    Parse the top-level ``esphome:`` block for ``(name, friendly_name, comment)``.

    Returns ``None`` for any field that isn't present in the YAML so
    callers can distinguish "key absent" (fall through to storage) from
    "explicit empty string" (user cleared the value).

    Resolves ``$var`` / ``${var}`` references in the captured fields
    against the file's top-level ``substitutions:`` block, so a config
    like::

        substitutions:
          friendly_name: "Living Room Lamp"
        esphome:
          friendly_name: $friendly_name

    yields ``friendly_name = "Living Room Lamp"`` instead of the raw
    ``$friendly_name`` token. Unknown references are left untouched.
    """
    name: str | None = None
    friendly_name: str | None = None
    comment: str | None = None
    substitutions: dict[str, str] = {}
    current_block: str | None = None

    for line in yaml_content.splitlines():
        if line and not line[0].isspace() and ":" in line:
            key = line.split(":")[0].strip()
            current_block = key if key in ("esphome", "substitutions") else None
            continue
        if current_block is None:
            continue
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        if current_block == "esphome":
            for field in ("name", "friendly_name", "comment"):
                prefix = f"{field}:"
                if stripped.startswith(prefix):
                    value = _parse_inline_value(stripped[len(prefix) :])
                    if field == "name":
                        name = value
                    elif field == "friendly_name":
                        friendly_name = value
                    else:
                        comment = value
                    break
        else:  # current_block == "substitutions"
            sub_key, sep, sub_raw = stripped.partition(":")
            if sep:
                substitutions[sub_key.strip()] = _parse_inline_value(sub_raw)

    if substitutions:
        name = _resolve_substitutions(name, substitutions)
        friendly_name = _resolve_substitutions(friendly_name, substitutions)
        comment = _resolve_substitutions(comment, substitutions)

    return name, friendly_name, comment


# ---------------------------------------------------------------------------
# Device construction
# ---------------------------------------------------------------------------


def load_device_from_storage(path: Path, board_id: str = "", ip: str = "") -> Device:
    """
    Build a Device model from a YAML config file and its StorageJSON.

    User-editable fields (name / friendly_name / comment) come from the
    YAML when present so the dashboard reflects edits immediately,
    without having to wait for the next compile to refresh StorageJSON.

    *ip* is the last-known resolved address from the device-builder
    metadata sidecar. Loading it back on startup lets the OTA address
    cache hand the CLI a usable IP before the first ping/mDNS sweep.
    """
    filename = path.name
    storage = StorageJSON.load(ext_storage_path(filename))

    try:
        yaml_content = path.read_text(encoding="utf-8")
    except OSError:
        yaml_content = ""
    yaml_name, yaml_friendly, yaml_comment = parse_esphome_meta(yaml_content)

    fallback_name = filename.removesuffix(".yml").removesuffix(".yaml")
    storage_name = storage.name if storage else None
    name = yaml_name or storage_name or fallback_name

    storage_friendly = storage.friendly_name if storage else None
    friendly_name = yaml_friendly if yaml_friendly is not None else (storage_friendly or name)

    storage_comment = storage.comment if storage else None
    comment = yaml_comment if yaml_comment is not None else storage_comment

    has_pending = True  # default: needs compile until we prove otherwise
    if storage and storage.firmware_bin_path and storage.firmware_bin_path.exists():
        yaml_mtime = path.stat().st_mtime
        bin_mtime = storage.firmware_bin_path.stat().st_mtime
        has_pending = yaml_mtime > bin_mtime

    deployed = storage.esphome_version or "" if storage else ""
    update_available = bool(deployed and deployed != const.__version__)

    target_platform = ""
    if storage and storage.target_platform:
        target_platform = storage.target_platform
    else:
        target_platform = detect_platform_from_yaml(path)

    return Device(
        name=name,
        friendly_name=friendly_name,
        configuration=filename,
        comment=comment,
        board_id=board_id,
        target_platform=target_platform,
        address=storage.address or "" if storage else "",
        ip=ip,
        web_port=storage.web_port if storage else None,
        current_version=const.__version__,
        deployed_version=deployed,
        loaded_integrations=sorted(storage.loaded_integrations) if storage else [],
        has_pending_changes=has_pending,
        update_available=update_available,
        uses_mqtt=device_uses_mqtt(yaml_content),
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _parse_inline_value(raw: str) -> str:
    """
    Clean a raw YAML scalar value.

    Strips an inline ``# comment`` and matching surrounding quotes.
    """
    value = raw.strip()
    if "#" in value and not (value.startswith('"') or value.startswith("'")):
        value = value.split("#", 1)[0].rstrip()
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        value = value[1:-1]
    return value


def _resolve_substitutions(value: str | None, subs: dict[str, str]) -> str | None:
    """
    Replace ``$var`` / ``${var}`` references in *value* with values from *subs*.

    Unknown references are left untouched (mirrors esphome's
    ``ignore_missing`` behaviour). Returns *value* unchanged when it
    is ``None`` or contains no references.
    """
    if value is None or "$" not in value:
        return value

    def repl(match: re.Match[str]) -> str:
        token = match.group(1)
        key = token[1:-1] if token.startswith("{") else token
        return subs.get(key, match.group(0))

    return _SUBSTITUTION_RE.sub(repl, value)
