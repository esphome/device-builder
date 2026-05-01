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

from esphome import const, yaml_util
from esphome.storage_json import StorageJSON, ext_storage_path

from ..models import Device, DeviceState

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


def yaml_has_top_level_block(yaml_content: str, key: str) -> bool:
    """Return True when the raw YAML literally declares a top-level *key*: block.

    Cheap line-scan that survives invalid drafts and partially edited
    configs — no full parse required. Misses configs that pull the
    block in via ``!include`` or packages, which is why scan-time
    flags prefer :func:`config_has_top_level_block` over the resolved
    config; this helper is the fallback for when YAML parsing fails
    (mid-edit drafts, missing secrets) so the indicator doesn't
    silently flip off while the user is typing.
    """
    for line in yaml_content.splitlines():
        if not line or line[0].isspace():
            continue
        stripped = line.strip()
        if stripped.startswith("#") or ":" not in stripped:
            continue
        if stripped.split(":", 1)[0].strip() == key:
            return True
    return False


def device_uses_mqtt(yaml_content: str) -> bool:
    """Return True when the raw YAML literally declares a top-level ``mqtt:`` block."""
    return yaml_has_top_level_block(yaml_content, "mqtt")


_RAW_API_ENCRYPTION_RE = re.compile(
    # Matches an ``encryption:`` line that's indented under ``api:``
    # (any depth ≥ 1 space). Used as a draft-time heuristic — once
    # ``load_device_yaml`` succeeds, the resolved-config check wins.
    #
    # The two body alternatives are exclusive: ``[ \t][^\n]*\n`` matches
    # an indented (non-blank) line; ``\n`` alone matches a literal blank
    # line. No overlap, so the engine can't backtrack between them on a
    # long run of newlines (the previous ``\s*\n`` alternative could
    # also consume a bare ``\n``, which CodeQL flagged as exponential).
    r"^api:[^\n]*\n(?:[ \t][^\n]*\n|\n)*[ \t]+encryption:(?:\s|$)",
    re.MULTILINE,
)


def yaml_has_api_encryption(yaml_content: str) -> bool:
    """Heuristic: True when raw YAML appears to declare ``api: encryption:``.

    Used during mid-edit drafts when the full resolver fails so the
    encryption-indicator doesn't blink off the moment the user types
    a syntax error. The resolved-config check is preferred whenever
    available (catches ``!include`` / packages this regex can't see).
    """
    return bool(_RAW_API_ENCRYPTION_RE.search(yaml_content))


def config_has_top_level_block(config: dict | None, key: str) -> bool:
    """Return True when *config* (a resolved device YAML) defines top-level *key*.

    Catches configs that split the block across ``!include`` / packages,
    which a raw-text scan misses. Treats the block as "present" when
    the key exists even with a ``None`` / empty value (e.g. a bare
    ``api:`` line is still an opt-in to the Native API).
    """
    return isinstance(config, dict) and key in config


def parse_esphome_meta(  # noqa: PLR0912
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


def load_device_from_storage(
    path: Path,
    board_id: str = "",
    ip: str = "",
    expected_config_hash: str = "",
    *,
    previous: Device | None = None,
) -> Device:
    """
    Build a Device model from a YAML config file and its StorageJSON.

    User-editable fields (name / friendly_name / comment) come from the
    YAML when present so the dashboard reflects edits immediately,
    without having to wait for the next compile to refresh StorageJSON.

    *ip* is the last-known resolved address from the device-builder
    metadata sidecar. Loading it back on startup lets the OTA address
    cache hand the CLI a usable IP before the first ping/mDNS sweep.

    *expected_config_hash* is the YAML's last-compiled config hash,
    typically read back from the metadata sidecar. Pair with the
    deployed hash from mDNS to tell "device runs the latest compile"
    apart from "device has older firmware"; empty when the device
    hasn't been compiled yet, in which case ``has_pending_changes``
    falls back to the mtime check.

    *previous* is the prior in-memory Device for this path, when one
    exists. Runtime-only fields populated by monitors (``state``,
    ``deployed_config_hash``) carry forward from it so a reload
    doesn't wipe what mDNS / ping has already discovered.
    """
    filename = path.name
    storage = StorageJSON.load(ext_storage_path(filename))

    try:
        yaml_content = path.read_text(encoding="utf-8")
    except OSError:
        yaml_content = ""
    yaml_name, yaml_friendly, yaml_comment = parse_esphome_meta(yaml_content)
    # Full resolved config (``!include`` / packages / ``!secret``
    # expanded) drives the api-encryption flag — a bare regex on raw
    # YAML would miss configs that pull the api block in via include
    # or split it across packages. ``None`` on parse failure is fine;
    # ``api_encrypted`` falls back to False.
    resolved_config = load_device_yaml(path)

    fallback_name = filename.removesuffix(".yml").removesuffix(".yaml")
    storage_name = storage.name if storage else None
    name = yaml_name or storage_name or fallback_name

    storage_friendly = storage.friendly_name if storage else None
    friendly_name = yaml_friendly if yaml_friendly is not None else (storage_friendly or name)

    storage_comment = storage.comment if storage else None
    comment = yaml_comment if yaml_comment is not None else storage_comment

    yaml_mtime = path.stat().st_mtime if path.exists() else None
    bin_mtime: float | None = None
    if storage and storage.firmware_bin_path and storage.firmware_bin_path.exists():
        bin_mtime = storage.firmware_bin_path.stat().st_mtime

    deployed_config_hash = previous.deployed_config_hash if previous else ""
    state = previous.state if previous else DeviceState.UNKNOWN

    has_pending = compute_has_pending_changes(
        yaml_mtime=yaml_mtime,
        bin_mtime=bin_mtime,
        expected_config_hash=expected_config_hash,
        deployed_config_hash=deployed_config_hash,
    )

    deployed = storage.esphome_version or "" if storage else ""
    update_available = bool(deployed and deployed != const.__version__)

    target_platform = ""
    if storage and storage.target_platform:
        target_platform = storage.target_platform
    else:
        target_platform = detect_platform_from_yaml(path)

    loaded_integrations = sorted(storage.loaded_integrations) if storage else []
    # ``api_enabled`` / ``api_encrypted`` get the union of every signal
    # we have:
    #   1. Resolved YAML config — catches local ``api:`` blocks pulled
    #      in via ``!include`` / local packages.
    #   2. Raw-text scan — keeps the indicator stable mid-edit when
    #      ``yaml_util.load_yaml`` fails on an invalid draft.
    #   3. ``StorageJSON.loaded_integrations`` — the compile-time
    #      ground truth. Required for configs that pull the api block
    #      in from a remote ``dashboard_import`` package (Apollo, etc.):
    #      ``yaml_util.load_yaml`` doesn't fetch URLs so the resolved
    #      config has no ``api:`` at the top level, but the compiled
    #      device still loads it.
    api_enabled = (
        ("api" in loaded_integrations)
        or config_has_top_level_block(resolved_config, "api")
        or yaml_has_top_level_block(yaml_content, "api")
    )
    api_encrypted = get_api_encryption_block(
        resolved_config
    ) is not None or yaml_has_api_encryption(yaml_content)
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
        expected_config_hash=expected_config_hash,
        deployed_config_hash=deployed_config_hash,
        loaded_integrations=loaded_integrations,
        state=state,
        has_pending_changes=has_pending,
        update_available=update_available,
        # ``uses_mqtt`` keeps its prior shape — the resolved config
        # wins, raw-text fills in mid-edit, and we don't have a
        # ``loaded_integrations`` entry that maps cleanly to "uses
        # mqtt for dashboard discovery" the way ``"api"`` does.
        uses_mqtt=(
            config_has_top_level_block(resolved_config, "mqtt")
            if resolved_config is not None
            else yaml_has_top_level_block(yaml_content, "mqtt")
        ),
        api_enabled=api_enabled,
        api_encrypted=api_encrypted,
    )


def compute_has_pending_changes(
    *,
    yaml_mtime: float | None,
    bin_mtime: float | None,
    expected_config_hash: str,
    deployed_config_hash: str,
) -> bool:
    """
    Decide whether a device's running firmware is out of sync with its YAML.

    Decision order, first match wins:

    1. No firmware binary on disk yet → pending.
    2. YAML edited after the last compile → pending. The mtime gate
       runs before any hash comparison so a stale ``expected`` from
       the prior compile can't accidentally match a deployed hash.
    3. Both ``expected_config_hash`` and ``deployed_config_hash``
       known → pending iff they differ. The deployed hash comes from
       mDNS (esphome/esphome#16145), the expected hash from the YAML's
       last compile; differing means the device is running older
       firmware than the latest compile (e.g. failed OTA, flashed
       elsewhere).
    4. Either hash missing → not pending. Devices on firmware that
       predates the ``config_hash`` TXT broadcast fall through here
       and stay quiet.
    """
    if bin_mtime is None:
        return True
    if yaml_mtime is not None and yaml_mtime > bin_mtime:
        return True
    if expected_config_hash and deployed_config_hash:
        return expected_config_hash != deployed_config_hash
    return False


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


def load_device_yaml(path: Path) -> dict | None:
    """Load *path* with ESPHome's YAML loader; return the top-level mapping.

    Resolves ``!secret`` / ``!include`` / etc. like a real compile, and
    returns ``None`` when the file isn't a mapping or fails to parse.
    Centralised so callers that need a parsed config — API-key
    extraction, encryption-status checks, future config inspection —
    share one entry point with the same error handling.
    """
    try:
        # ``yaml_util.load_yaml`` calls ``.open()`` on its argument, so
        # pass the ``Path`` directly — handing it a stringified path
        # raises ``AttributeError`` deep inside the loader.
        config = yaml_util.load_yaml(path)
    except Exception:
        return None
    return config if isinstance(config, dict) else None


def get_api_encryption_block(config: dict | None) -> dict | None:
    """
    Return the ``api.encryption`` mapping from a parsed device config.

    ``None`` when the config is missing, has no ``api:`` block, or the
    ``api:`` block has no ``encryption:`` sub-mapping. Useful for both
    the "is encrypted?" boolean and the "show me the key" string —
    they share the same lookup, the only thing that differs is what
    they pull off the result.
    """
    if not isinstance(config, dict):
        return None
    api_block = config.get("api")
    if not isinstance(api_block, dict):
        return None
    encryption = api_block.get("encryption")
    return encryption if isinstance(encryption, dict) else None


def get_api_encryption_key(config: dict | None) -> str:
    """Return the resolved Native API encryption key, or empty string."""
    encryption = get_api_encryption_block(config)
    if encryption is None:
        return ""
    key = encryption.get("key")
    return key if isinstance(key, str) else ""


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
