"""
Pure-function helpers for generating, parsing, and reading device YAML.

These utilities are intentionally state-free so they can be reused by
the devices controller, the device builder, and any future tool that
needs to inspect or synthesise an ESPHome config without instantiating
a controller.
"""

from __future__ import annotations

import base64
import logging
import re
import secrets
from pathlib import Path
from typing import TYPE_CHECKING

from esphome import const, yaml_util
from esphome.const import CONF_PACKAGES
from esphome.storage_json import StorageJSON, ext_storage_path

# Prefer the central dispatcher landing in esphome/esphome#16300
# so we depend on a stable upstream API rather than reaching into
# ``NO_WIFI_VARIANTS`` / ``BOARDS`` implementation details. When
# the upstream helper is available the fallback constants below
# stay unimported — the "new ESPHome" path has zero coupling to
# upstream internals. The implementation-detail imports + derived
# frozenset only happen on the ``except ImportError`` branch,
# which covers every esphome we currently support. Once the
# floor moves past the release that ships #16300 this whole
# block collapses to a plain import.
try:
    from esphome.components.wifi import has_native_wifi as _esphome_has_native_wifi

    _ESPHOME_RP2040_BOARDS: dict[str, dict] | None = None
    _ESP32_NO_WIFI_VARIANTS: frozenset[str] = frozenset()
except ImportError:
    _esphome_has_native_wifi = None  # type: ignore[assignment]
    from esphome.components.rp2040.boards import (
        BOARDS as _ESPHOME_RP2040_BOARDS,  # type: ignore[assignment]
    )
    from esphome.components.wifi import (
        NO_WIFI_VARIANTS as _ESPHOME_NO_WIFI_VARIANTS,
    )

    # ESPHome stores the variant tags in canonical uppercase
    # (``"ESP32H2"``); the wizard compares against the lowercase
    # ``Esp32Variant`` enum value, so normalise once at module
    # load.
    _ESP32_NO_WIFI_VARIANTS = frozenset(v.lower() for v in _ESPHOME_NO_WIFI_VARIANTS)

# Prefer the upstream single-call seam when present (the
# ``resolve_packages`` proposal landing as esphome/esphome#16235).
# Fall back to the two-step ``do_packages_pass`` + ``merge_packages``
# that ESPHome's own ``validate_config`` pipeline strings together
# today. Both imports are guarded by ``try/except ImportError``:
# a future esphome that ships only ``resolve_packages`` (deprecating
# or moving the two-step helpers) would otherwise break our module-
# load. Once the dashboard's dep floor moves past the release that
# shipped ``resolve_packages``, the entire fallback path can be
# deleted in one commit.
try:
    from esphome.components.packages import (  # type: ignore[attr-defined]
        resolve_packages as _resolve_packages,
    )
except ImportError:
    _resolve_packages = None

try:
    from esphome.components.packages import (
        do_packages_pass as _do_packages_pass,
    )
    from esphome.components.packages import (
        merge_packages as _merge_packages,
    )
except ImportError:
    _do_packages_pass = None  # type: ignore[assignment]
    _merge_packages = None  # type: ignore[assignment]

from ..models import Device, DeviceState
from .mac_addresses import derive_interface_macs

_LOGGER = logging.getLogger(__name__)


if TYPE_CHECKING:
    from collections.abc import Callable

    from ..models import BoardCatalogEntry

_PLATFORM_KEYS = frozenset({"esp32", "esp8266", "rp2040", "bk72xx", "rtl87xx", "ln882x", "nrf52"})

# Wi-Fi-first families for the fallback dispatcher's allowlist —
# mirrors upstream's ``_WIFI_FIRST_PLATFORMS`` so the wizard's
# behaviour stays identical whether the upstream helper is
# available or not. Includes ``libretiny`` (the legacy umbrella
# key for the bk72xx / rtl87xx / ln882x families) so old configs
# that haven't migrated to the per-family keys still resolve.
_FALLBACK_WIFI_FIRST_PLATFORMS: frozenset[str] = frozenset(
    {"esp8266", "bk72xx", "rtl87xx", "ln882x", "libretiny"}
)

# TODO comment block emitted by ``generate_device_yaml`` for
# no-Wi-Fi boards (H2 / P4 / plain Pico / etc.) instead of
# ``api:`` + ``ota:``. Lifted to module scope so the generator
# can ``lines.extend`` rather than five inline ``lines.append``
# calls — keeps the function under PLR0915's statement budget.
_NO_NETWORK_TODO_LINES: tuple[str, ...] = (
    "# This board has no native Wi-Fi. ESPHome's ``api:`` and",
    "# ``ota:`` components both require a ``network``",
    "# component — configure ``openthread:`` / ``ethernet:`` /",
    "# ``esp32_hosted:`` to suit your setup, then add ``api:``",
    "# and ``ota:`` blocks once the network is ready.",
    "",
)


def _fallback_has_native_wifi(
    *, platform: str, board: str | None = None, variant: str | None = None
) -> bool:
    """Pure-Python fallback for ``esphome.components.wifi.has_native_wifi``.

    Mirrors the upstream dispatcher's contract — including the
    allowlist semantics for unknown / Wi-Fi-less platforms
    (``host``, ``nrf52``, future additions) so the wizard's
    behaviour stays identical whether the upstream helper is
    available or not.
    """
    if platform == "esp32":
        return not (variant and variant.lower() in _ESP32_NO_WIFI_VARIANTS)
    if platform == "rp2040":
        if board is None:
            return True
        info = _ESPHOME_RP2040_BOARDS.get(board)
        return True if info is None else info.get("wifi", False)
    return platform in _FALLBACK_WIFI_FIRST_PLATFORMS


def _select_wifi_helper(
    upstream: Callable[..., bool] | None,
) -> Callable[..., bool]:
    """Pick the upstream dispatcher when available, the fallback otherwise.

    Factored out so tests can exercise both branches without
    reloading the module — pass ``None`` to force the fallback
    path, pass a callable to force the upstream path. The
    module-level invocation below uses whatever the import-time
    ``try/except`` produced.
    """
    return upstream or _fallback_has_native_wifi


# Alias to the upstream helper when present, the fallback otherwise.
# ``_infer_native_wifi`` calls through this single alias.
_has_native_wifi = _select_wifi_helper(_esphome_has_native_wifi)


# Mirrors esphome's substitution regex (`config_validation.VARIABLE_PROG`):
# matches ``$name`` or ``${name}`` where name is alphanumeric + underscore.
_SUBSTITUTION_RE = re.compile(r"\$(\{[a-zA-Z0-9_]*\}|[a-zA-Z0-9_]+)")

# Cap on recursive substitution passes — protects against circular
# references (``a: ${b}`` / ``b: ${a}``) without bailing on legitimately
# deep chains a user might write.
_SUBSTITUTION_MAX_PASSES = 16

# ESPHome's ``esphome.name`` accepts lowercase ASCII letters, digits,
# and hyphens — the same character class an mDNS hostname / API
# endpoint can carry. A parsed value with anything else (dots, spaces,
# uppercase, ...) means we picked up the wrong field (a package id, a
# friendly_name leaked through, etc.) and should be rejected so it
# doesn't end up as the catalog key.
_VALID_ESPHOME_NAME_RE = re.compile(r"\A[a-z0-9-]+\Z")


def _is_valid_esphome_name(value: str) -> bool:
    """Return True when *value* matches ESPHome's ``esphome.name`` shape."""
    return bool(_VALID_ESPHOME_NAME_RE.match(value))


# ---------------------------------------------------------------------------
# Configuration filename helpers
# ---------------------------------------------------------------------------


def configuration_stem(configuration: str) -> str:
    """
    Strip the ``.yaml`` / ``.yml`` extension off a configuration filename.

    The stem is the device's identity for almost every comparison
    we care about — it drives the mDNS hostname (``<stem>.local``),
    the StorageJSON key, the build-dir name. Filename-level
    comparisons (``a.yaml == b.yaml``) miss the case where one side
    uses ``.yml``; comparing stems treats both extensions as the
    same device.
    """
    return configuration.removesuffix(".yaml").removesuffix(".yml")


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

    # Wi-Fi decision — used both for the ``wifi:`` block below and to
    # gate ``api:`` / ``ota:`` (both DEPENDENCIES=["network"], so
    # they can't compile on a board without a network component
    # auto-loaded by ``wifi:`` / ``ethernet:`` / ``openthread:`` /
    # ``host:``). Prefer the manifest's explicit ``connectivity``
    # claim, fall back to a platform/variant/board-aware inference
    # for boards whose hardware block omits ``connectivity``
    # entirely. The inference asks ESPHome's own ``NO_WIFI_VARIANTS``
    # / ``rp2040.boards.BOARDS`` so a future no-Wi-Fi variant or new
    # RP2040 Wi-Fi board flows through without a coordinated edit
    # here.
    connectivity = [c.value for c in board.hardware.connectivity] if board.hardware else []
    has_wifi = "wifi" in connectivity if connectivity else _infer_native_wifi(board)

    if has_wifi:
        # Home Assistant API — unique encryption key per device.
        # Skipped on no-Wi-Fi boards because ``api:`` requires a
        # ``network`` component (DEPENDENCIES=["network"]) and the
        # wizard doesn't emit ``ethernet:`` / ``openthread:`` /
        # ``host:`` for non-Wi-Fi boards. Validation would otherwise
        # reject the generated config with
        # "Component api requires component network." — see ``ota``
        # below for the same reasoning.
        api_key = base64.b64encode(secrets.token_bytes(32)).decode()
        lines.append("api:")
        lines.append("  encryption:")
        lines.append(f'    key: "{api_key}"')
        lines.append("")

        # OTA — same network dependency as ``api:`` above.
        lines.append("ota:")
        lines.append("  - platform: esphome")
        lines.append("")

        lines.append("wifi:")
        if ssid:
            lines.append(f"  ssid: {ssid}")
            lines.append(f"  password: {psk}")
        else:
            lines.append("  ssid: !secret wifi_ssid")
            lines.append("  password: !secret wifi_password")
        lines.append("")
    else:
        # No native Wi-Fi → leave a TODO so the user knows what they
        # need to configure before adding ``api:`` / ``ota:``. Both
        # require a ``network`` component to compile, and the right
        # network for these boards depends on the user's setup
        # (``openthread:`` for H2, ``ethernet:`` for P4 with a
        # co-processor, ``esp32_hosted:`` for either with a Wi-Fi
        # daughterboard, etc.). Emitting a placeholder block would
        # bake an arbitrary choice into the generated YAML; a
        # commented-out hint lets the user pick.
        lines.extend(_NO_NETWORK_TODO_LINES)

    return "\n".join(lines)


def _infer_native_wifi(board: BoardCatalogEntry) -> bool:
    """Decide whether *board* has native Wi-Fi when its manifest is silent.

    Used by :func:`generate_device_yaml` only when the manifest's
    ``hardware.connectivity`` is empty — when the manifest claims a
    list explicitly we honour it. The inference walks the
    platform/variant/board chain so future curated manifests that
    forget the connectivity claim still produce a compilable config:

    1. Platform ``esp32`` + variant in ESPHome's ``NO_WIFI_VARIANTS``
       (currently ``esp32h2`` / ``esp32p4``) → False.
    2. Platform ``rp2040`` → True only when the PlatformIO board id
       is in ESPHome's RP2040 ``BOARDS`` table marked ``"wifi": True``
       (the Pico W / Pico 2 W / Pimoroni / SparkFun / Waveshare W
       variants — the plain Pico, plain Pico 2, Seeed XIAO RP2040,
       Waveshare RP2040 Zero, etc. fall on the False side here).
    3. Wi-Fi-first families (``esp8266`` / ``bk72xx`` / ``rtl87xx``
       / ``ln882x`` / ``libretiny``) plus the catch-all ESP32
       case → True. Allowlist-based: ``nrf52`` (BLE-only),
       ``host`` (host-binary build, no radio), and any platform
       not on the allowlist → False, so a future ESPHome platform
       missed here fails closed in the wizard rather than silently
       emitting a ``wifi:`` block the new platform's component
       would reject.

    The dispatch goes through ``_has_native_wifi`` — a module-level
    alias that prefers the upstream
    ``esphome.components.wifi.has_native_wifi`` central dispatcher
    (landing in esphome/esphome#16300) and falls back to a
    pure-Python equivalent derived from ``NO_WIFI_VARIANTS`` /
    ``BOARDS`` when the upstream helper isn't available. The
    upstream dispatcher knows about every platform ESPHome
    supports, so a new platform added there flows through to the
    wizard automatically — no per-platform switch maintained here.
    """
    esphome_cfg = board.esphome
    # ``str(...)`` handles both the production enum (``Platform`` /
    # ``Esp32Variant`` are ``StrEnum``) and bare-string inputs from
    # tests that mock the catalog entry without going through the
    # enum constructors.
    return _has_native_wifi(
        platform=str(esphome_cfg.platform) if esphome_cfg.platform else "",
        board=esphome_cfg.board,
        variant=str(esphome_cfg.variant) if esphome_cfg.variant else None,
    )


def generate_minimal_stub_yaml(name: str, friendly_name: str) -> str:
    """
    Render a minimal ``esphome rename``-compatible stub config.

    Used by the wizard's "Empty Configuration — for manually
    writing or pasting a configuration" path, where the user
    wants a starter to fully rewrite. The output validates as-is
    against ESPHome's schema (so every downstream operation —
    rename, edit_friendly_name, install — accepts it) but is
    intentionally minimal so the user can swap the platform
    block without unwinding wizard-specific defaults like an
    auto-generated API encryption key.

    The platform defaults to ``esp32`` with ``board: esp32dev``
    because esp32 is the most common starter target and
    ``esp32dev`` is upstream-canonical (ships in
    ``esphome.const.PLATFORMIO_ESP32_LUT`` and validates without
    the catalog). The leading comment tells the user to replace
    the platform block if their hardware differs, so the silent-
    bind concern is at least called out in the file the user is
    about to edit.
    """
    api_key = base64.b64encode(secrets.token_bytes(32)).decode()
    return (
        f"esphome:\n  name: {name}\n  friendly_name: {friendly_name}\n\n"
        "# Replace this with your actual platform if you aren't using ESP32.\n"
        "esp32:\n  board: esp32dev\n\n"
        "logger:\n\n"
        "api:\n  encryption:\n"
        f'    key: "{api_key}"\n\n'
        "ota:\n  - platform: esphome\n\n"
        "wifi:\n"
        "  ssid: !secret wifi_ssid\n"
        "  password: !secret wifi_password\n"
    )


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
    Find a YAML file's platform key.

    First tries the cheap line-scan against the raw file (no parser
    involved, survives mid-edit drafts). Falls back to the
    package-merged load ONLY when the raw scan misses AND the file
    actually contains a ``packages:`` block — configs that place
    ``esp32:`` / ``esp8266:`` / etc. inside a package only register
    that key after merge runs. Without the gate every YAML that
    happens to omit a top-level platform key (mid-edit drafts,
    package-less configs that get their platform from
    ``StorageJSON``) would pay a full parser load on every
    dashboard scan, even though there's nothing in the file the
    merge could surface. The cheap-regex-only fast path stays the
    winner for the typical no-packages config.

    Returns the empty string when neither path turns up a platform.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception:
        return ""
    try:
        platform, _, _ = parse_platform_from_yaml(raw)
    except Exception:
        platform = ""
    if platform:
        return platform
    if not yaml_has_top_level_block(raw, CONF_PACKAGES):
        # No ``packages:`` block in the raw text → the merge can't
        # surface a platform key that wasn't already there. Skip
        # the load to keep the scan cheap.
        return ""
    config = load_device_yaml(path)
    if isinstance(config, dict):
        for key in config:
            if key in _PLATFORM_KEYS:
                return key
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


def extract_directly_referenced_integrations(
    config: dict | None,
) -> list[str]:
    """
    Return the sorted list of directly-written integration names.

    Walks a resolved device config and pulls out top-level keys
    plus platform stems from ``- platform: <name>`` (or single-form
    ``platform: <name>``) references.

    The complement of this set against ``StorageJSON.loaded_integrations``
    is the auto-loaded dependency chain (``md5`` pulled in by WPA2
    password hashing, ``mdns`` by ``api``, ``web_server_base`` by
    ``web_server``, ``voltage_sampler`` by ADC sensors, …). The
    frontend's device-drawer uses the split to surface direct
    integrations as the primary list and tuck auto-loaded ones
    behind a collapsible — see issue #422.

    Resolved config (``!include`` / packages expanded) is the right
    source: a package the user imported counts as direct (they
    chose the package), while integrations the platform infrastructure
    auto-loads as dependencies of those imports are indirect.
    Returns ``[]`` for ``None`` (resolved-parse failed) so the
    frontend falls through to the flat-list rendering.

    Two YAML shapes carry platform refs:

    1. List-of-platforms (the common case)::

        sensor:
          - platform: bme280_i2c
          - platform: dht

       → adds ``sensor`` (top-level key) plus ``bme280_i2c`` and
       ``dht`` (each item's ``platform`` value).

    2. Single-platform dict (``ota`` / ``mqtt`` historically)::

        ota:
          platform: esphome

       → adds ``ota`` plus ``esphome``.

    Non-string ``platform:`` values (templated lambdas, malformed
    drafts) are skipped silently rather than emitting garbage names.
    """
    if not isinstance(config, dict):
        return []
    out: set[str] = set()
    for key, value in config.items():
        if not isinstance(key, str):
            continue
        out.add(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    platform = item.get("platform")
                    if isinstance(platform, str) and platform:
                        out.add(platform)
        elif isinstance(value, dict):
            platform = value.get("platform")
            if isinstance(platform, str) and platform:
                out.add(platform)
    return sorted(out)


def parse_esphome_meta(  # noqa: PLR0912
    yaml_content: str,
) -> tuple[str | None, str | None, str | None, str | None]:
    """
    Parse the top-level ``esphome:`` block for ``(name, friendly_name, comment, area)``.

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
    area: str | None = None
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
            for field in ("name", "friendly_name", "comment", "area"):
                prefix = f"{field}:"
                if stripped.startswith(prefix):
                    value = _parse_inline_value(stripped[len(prefix) :])
                    if field == "name":
                        name = value
                    elif field == "friendly_name":
                        friendly_name = value
                    elif field == "comment":
                        comment = value
                    else:
                        area = value
                    break
        else:  # current_block == "substitutions"
            sub_key, sep, sub_raw = stripped.partition(":")
            if sep:
                substitutions[sub_key.strip()] = _parse_inline_value(sub_raw)

    if substitutions:
        name = _resolve_substitutions(name, substitutions)
        friendly_name = _resolve_substitutions(friendly_name, substitutions)
        comment = _resolve_substitutions(comment, substitutions)
        area = _resolve_substitutions(area, substitutions)

    return name, friendly_name, comment, area


# ---------------------------------------------------------------------------
# Device construction
# ---------------------------------------------------------------------------


def load_device_from_storage(
    path: Path,
    board_id: str = "",
    ip: str = "",
    expected_config_hash: str = "",
    mac_address: str = "",
    build_size_bytes: int = 0,
    labels: tuple[str, ...] = (),
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

    *mac_address* is the canonical ``XX:XX:XX:XX:XX:XX`` MAC
    observed in the device's mDNS ``mac`` TXT (normalized at
    ingest), persisted to the sidecar so the drawer / table
    render the address immediately on startup — ESPHome devices
    stay mDNS-silent until probed, and the sidecar bridges the gap
    until the discovery sweep prompts a fresh announcement.

    *build_size_bytes* is the cached total size of the per-device
    ``.esphome/build/<name>/`` tree at the freshness pair
    (build-dir mtime + ``build_info.json`` mtime) captured by the
    last walk. Threaded through the scanner so the drawer / table
    render the size immediately on startup; recomputation is
    driven by :class:`BuildSizeRefresher` (the single-worker
    refresh queue) gated on the freshness-pair equality check, so
    a steady-state re-scan stays off the heavy I/O path.

    *labels* is the per-device list of label IDs (opaque
    ``uuid.uuid4().hex`` references into the global ``_labels``
    catalog at ``.device-builder.json``). The metadata round-trip
    is the source of truth, so a reload triggered by an unrelated
    YAML edit picks up label changes the same way it picks up
    board-id edits.

    *previous* is the prior in-memory Device for this path, when one
    exists. Runtime-only fields populated by monitors (``state``,
    ``deployed_config_hash``, ``ip_addresses``,
    ``api_encryption_active``) carry forward from it so a reload
    doesn't wipe what mDNS / ping has already discovered.
    """
    filename = path.name
    storage = StorageJSON.load(ext_storage_path(filename))

    try:
        yaml_content = path.read_text(encoding="utf-8")
    except OSError:
        yaml_content = ""
    yaml_name, yaml_friendly, yaml_comment, yaml_area = parse_esphome_meta(yaml_content)
    # Full resolved config (``!include`` / packages / ``!secret``
    # expanded) drives the api-encryption flag — a bare regex on raw
    # YAML would miss configs that pull the api block in via include
    # or split it across packages. ``None`` on parse failure is fine;
    # ``api_encrypted`` falls back to False.
    resolved_config = load_device_yaml(path)

    fallback_name = configuration_stem(filename)
    storage_name = storage.name if storage else None
    # Pick the first valid ESPHome slug from (yaml_name, storage_name).
    # Real ESPHome ``esphome.name`` values are ``[a-z0-9-]+`` — a parsed
    # value with dots / spaces / uppercase is something else (a package
    # id like ``ratgdo.esphome``, a friendly_name leaked through, etc.).
    # ``device.name`` is the key the state monitor uses to match mDNS
    # announcements, so duplicates across multiple YAMLs (which is what
    # happens when several configs share the same ``dashboard_import``
    # package) collapse all those devices onto a single Device row.
    # Falling back to the filename when the parsed name is invalid
    # keeps the catalog key unique.
    name = next(
        (n for n in (yaml_name, storage_name) if n and _is_valid_esphome_name(n)),
        fallback_name,
    )

    storage_friendly = storage.friendly_name if storage else None
    friendly_name = yaml_friendly if yaml_friendly is not None else (storage_friendly or name)

    storage_comment = storage.comment if storage else None
    comment = yaml_comment if yaml_comment is not None else storage_comment

    # ``esphome.area`` only lives in the YAML — StorageJSON doesn't carry
    # it, so there's no fallback. Empty string when absent.
    area = yaml_area or ""

    yaml_mtime = path.stat().st_mtime if path.exists() else None
    bin_mtime: float | None = None
    if storage and storage.firmware_bin_path and storage.firmware_bin_path.exists():
        bin_mtime = storage.firmware_bin_path.stat().st_mtime

    deployed_config_hash = previous.deployed_config_hash if previous else ""
    state = previous.state if previous else DeviceState.UNKNOWN
    # mDNS-derived view that isn't persisted in the metadata sidecar;
    # carry it across reloads so a re-scan triggered by an unrelated
    # YAML edit doesn't blank the dashboard's IP list until the next
    # mDNS broadcast lands.
    ip_addresses = list(previous.ip_addresses) if previous else []
    # Same carry-forward rule for the encryption-active observation:
    # the next ``_esphomelib._tcp`` announce can be a couple of TTLs
    # (minutes) away, and a scanner reload triggered by a flash /
    # YAML edit / ``--only-generate`` between announces would
    # otherwise wipe a previously-truthy ``api_encryption_active``
    # back to ``None``. The frontend's ``getEncryptionState`` reads
    # ``None`` as "mDNS not seen yet" and combines it with
    # ``has_pending_changes`` to render a "Pending install" chip —
    # so the user sees a freshly-flashed encrypted device flip into
    # the warning state for the gap window despite the firmware on
    # the wire still broadcasting encryption. The
    # ``apply_api_encryption`` path follows the "Device is the
    # source of truth" rule established in PR #75 and dedupes
    # against ``device.api_encryption_active`` (not a monitor-side
    # cache), so seeding the field from ``previous`` doesn't break
    # the "next mDNS announce corrects mismatched state" guarantee
    # — a re-announce with a different value still hits the
    # callback path.
    api_encryption_active = previous.api_encryption_active if previous else None

    has_pending = compute_has_pending_changes(
        yaml_mtime=yaml_mtime,
        bin_mtime=bin_mtime,
        expected_config_hash=expected_config_hash,
        deployed_config_hash=deployed_config_hash,
    )

    deployed = storage.esphome_version or "" if storage else ""
    update_available = bool(deployed and deployed != const.__version__)

    # ``Device.target_platform`` is the lowercase platform *key*
    # (``esp32``, ``esp8266``, ``rp2040``, …) — the value the
    # frontend's PLATFORM column renders. Source order:
    #
    # 1. ``storage.core_platform`` — post-codegen ground truth
    #    written by upstream's ``StorageJSON.from_esphome_core``
    #    (esphome#9028, 2025.6+). Always the lowercase platform
    #    key, never the chip variant. ``getattr`` with a default
    #    keeps this working on older ``esphome`` installs where
    #    ``StorageJSON`` doesn't carry the attribute at all —
    #    pyproject's floor is ``esphome>=2024.1.0`` so the
    #    pre-#9028 path is reachable.
    # 2. ``detect_platform_from_yaml(path)`` — the YAML's top-level
    #    platform key, also lowercase. Picks up never-compiled
    #    devices and pre-2025.6 ``StorageJSON`` files that don't
    #    carry ``core_platform`` yet.
    #
    # ``storage.target_platform`` is deliberately NOT a fallback:
    # upstream uppercases it AND resolves it to the chip variant
    # for ESP32 boards (``ESP32S3``, ``ESP32C3``), so a fleet with
    # mixed compile states would otherwise show ``esp32s3`` next
    # to ``esp32`` for the same family — the inconsistency
    # frontend issue #137 was opened against. Chip-variant info
    # for ``_verify_chip`` is read from StorageJSON directly at
    # the call site, where the variant *is* the right level of
    # detail.
    target_platform = ""
    if storage:
        core_platform = getattr(storage, "core_platform", None)
        if core_platform:
            target_platform = core_platform.lower()
    if not target_platform:
        target_platform = detect_platform_from_yaml(path)

    loaded_integrations = sorted(storage.loaded_integrations) if storage else []
    # Subset of loaded_integrations the user directly wrote — top-
    # level keys + ``- platform:`` stems. Frontend's device-drawer
    # splits the loaded list into "direct" (these) and "indirect"
    # (the rest, all auto-loaded dependencies). Resolved config is
    # the right source so package contents are direct (the user
    # imported them); falls back to the empty list when resolved
    # config is unavailable so the frontend can render the flat
    # loaded list as a graceful degrade. Issue #422.
    user_referenced = set(extract_directly_referenced_integrations(resolved_config))
    directly_referenced_integrations = [
        name for name in loaded_integrations if name in user_referenced
    ]
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
    # Derived interface MACs: deterministic from
    # ``mac_address`` + ``target_platform`` + ``loaded_integrations``,
    # so we recompute on construction rather than persisting in the
    # sidecar — a YAML edit that toggles bluetooth picks up the new
    # derived MAC on the next reload, no stale-cache window.
    ethernet_mac, bluetooth_mac = derive_interface_macs(
        mac_address, target_platform, loaded_integrations
    )
    api_encrypted = get_api_encryption_block(
        resolved_config
    ) is not None or yaml_has_api_encryption(yaml_content)
    return Device(
        name=name,
        friendly_name=friendly_name,
        configuration=filename,
        comment=comment,
        area=area,
        board_id=board_id,
        target_platform=target_platform,
        # StorageJSON only exists after a successful compile, so a
        # freshly-added (or never-built) device would otherwise carry
        # an empty ``address`` and fall out of the ping sweep — stuck
        # in UNKNOWN forever. Fall back to ``<filename-stem>.local``
        # (NOT ``<name>.local``): the filename is canonical and
        # matches what the user types, while ``name`` is parsed from
        # YAML and can come back as a friendly_name or a package
        # import URL when the YAML doesn't carry a slug-shaped
        # ``esphome.name`` (e.g. configs that get the name from a
        # remote ``dashboard_import`` package). The scanner refreshes
        # this on the next compile if the device picks a different
        # ``esphome.address``.
        address=(storage.address if storage and storage.address else f"{fallback_name}.local"),
        ip=ip,
        ip_addresses=ip_addresses,
        web_port=storage.web_port if storage else None,
        current_version=const.__version__,
        deployed_version=deployed,
        expected_config_hash=expected_config_hash,
        deployed_config_hash=deployed_config_hash,
        loaded_integrations=loaded_integrations,
        directly_referenced_integrations=directly_referenced_integrations,
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
        api_encryption_active=api_encryption_active,
        mac_address=mac_address,
        ethernet_mac=ethernet_mac,
        bluetooth_mac=bluetooth_mac,
        build_size_bytes=build_size_bytes,
        labels=list(labels),
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

    1. Both ``expected_config_hash`` and ``deployed_config_hash``
       known → pending iff they differ. The deployed hash comes from
       mDNS (esphome/esphome#16145), the expected hash is read from
       ``build_info.json`` (firmware-canonical, post-codegen). The
       hash comparison is authoritative for any device on
       broadcast-capable firmware: if they match, the running
       firmware is built from the same logical config the YAML now
       resolves to, even if the YAML's mtime ticked forward
       (whitespace-only / comment-only edits, ``--only-generate``
       rewriting StorageJSON, etc.) or the local ``firmware.bin``
       was wiped (``clean`` job, fresh checkout, ``--only-generate``
       only). If they differ, the device is running older firmware
       than the latest compile — failed OTA, flashed elsewhere, etc.
    2. Hashes aren't both known and there's no firmware binary on
       disk yet → pending. We don't have a comparable pair of
       hashes (either the YAML's never been compiled or the device
       isn't broadcasting), and we don't even have a local artefact
       to mtime-check against, so we definitionally have unflushed
       edits.
    3. YAML edited after the last compile → pending. Mtime is the
       fallback for devices that pre-date the ``config_hash`` TXT
       broadcast or for the brief window between an edit and the
       background ``--only-generate`` updating
       ``expected_config_hash``.
    4. Otherwise → not pending. Devices on firmware that predates
       the broadcast and haven't been edited stay quiet.
    """
    if expected_config_hash and deployed_config_hash:
        return expected_config_hash != deployed_config_hash
    if bin_mtime is None:
        return True
    return yaml_mtime is not None and yaml_mtime > bin_mtime


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

    Resolves ``!secret`` / ``!include`` / etc. like a real compile,
    flattens any ``packages:`` block into the main config via
    ESPHome's package-resolution internals (the single-call
    ``resolve_packages`` on newer ESPHome, the two-step
    ``do_packages_pass`` + ``merge_packages`` fallback on older
    builds), so callers see what the compiler actually sees —
    ``api:`` / ``wifi:`` / target-platform blocks contributed by
    packages register as top-level keys here — and returns
    ``None`` when the file isn't a mapping or fails to parse.

    Package resolution is best-effort: a remote (git) package needs
    network access, an invalid package definition fails ESPHome's
    voluptuous validator, etc. When the package pass throws we keep
    the unmerged config rather than dropping it — better to surface
    the local YAML than nothing, and the unmerged shape was the
    pre-fix behaviour the dashboard already handled.

    Centralised so callers that need a parsed config — API-key
    extraction, encryption-status checks, top-level-block detection
    used by the device-card flags — share one entry point with the
    same error handling and the same package-merge contract.
    """
    try:
        # ``yaml_util.load_yaml`` calls ``.open()`` on its argument, so
        # pass the ``Path`` directly — handing it a stringified path
        # raises ``AttributeError`` deep inside the loader.
        config = yaml_util.load_yaml(path)
    except Exception:
        return None
    if not isinstance(config, dict):
        return None
    # ``packages:`` is a separate pass in the ESPHome pipeline (see
    # ``do_packages_pass`` + ``merge_packages`` in
    # ``esphome.config.validate_config``): packages need to be loaded
    # and merged so blocks they contribute (api / wifi / target-platform
    # / …) become top-level keys. Without this step a config that
    # puts those blocks behind ``packages:`` comes back from
    # ``yaml_util.load_yaml`` with everything still nested under
    # ``packages:``, and the dashboard's flag detection silently
    # misses them. We delegate to ESPHome internals — the loader
    # and merge algorithm live upstream.
    #
    # ``load_device_yaml`` runs on a worker thread (the device
    # scanner uses ``run_in_executor``; the devices controller
    # threads it through too), NOT the WS event loop. Same trade
    # the validate path lives with: ESPHome's ``vscode`` subprocess
    # runs the full resolve on every validate call, so a
    # remote-package config that fires ``git clone`` synchronously
    # blocks this worker thread for as long as the clone takes
    # (minutes on a slow connection / large repo) but the dashboard
    # stays responsive to other clients. The follow-up that mirrors
    # the validate-style subprocess pattern for metadata refresh
    # (so a slow remote clone doesn't stall the whole scan) is
    # tracked separately; this PR closes the local-package gap
    # behind #288 without trying to solve the remote-package
    # latency problem at the same time.
    if isinstance(config.get(CONF_PACKAGES), (dict, list)):
        try:
            if _resolve_packages is not None:
                config = _resolve_packages(config)
            elif _do_packages_pass is not None and _merge_packages is not None:
                config = _do_packages_pass(config)
                config = _merge_packages(config)
        except Exception:
            # Best-effort: a bad / unreachable package shouldn't
            # blank the device's metadata. Keep the unmerged shape
            # so the raw-YAML fallback paths at the call sites
            # still work.
            _LOGGER.debug(
                "Package merge failed for %s; using unmerged config",
                path,
                exc_info=True,
            )
    return config


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

    Substitutions are expanded recursively so a substitution whose value
    itself references another substitution (e.g. ``comment: "${area}, Well"``
    paired with ``esphome.comment: ${comment}``) resolves to the fully
    substituted string. Unknown references are left untouched (mirrors
    esphome's ``ignore_missing`` behaviour). Returns *value* unchanged when
    it is ``None`` or contains no references.
    """
    if value is None or "$" not in value:
        return value

    def repl(match: re.Match[str]) -> str:
        token = match.group(1)
        key = token[1:-1] if token.startswith("{") else token
        return subs.get(key, match.group(0))

    # Re-run until the string stops changing — a single ``re.sub`` pass
    # only walks the input once, so a reference whose replacement value
    # contains another reference would otherwise be left half-resolved.
    # Bounded to defend against circular substitutions.
    for _ in range(_SUBSTITUTION_MAX_PASSES):
        previous = value
        value = _SUBSTITUTION_RE.sub(repl, value)
        if value == previous or "$" not in value:
            break

    return value
