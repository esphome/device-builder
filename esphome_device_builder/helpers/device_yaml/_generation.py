"""Generate complete device YAML and minimal stubs from board definitions."""

from __future__ import annotations

import base64
import secrets
import string
from typing import TYPE_CHECKING, Any

from ...definitions import load_platform_capabilities_index
from ..yaml import _safe_yaml_scalar, merge_component_yaml

if TYPE_CHECKING:
    from ...models import BoardCatalogEntry, ComponentCatalogEntry


# Native-Wi-Fi capability, snapshotted from esphome into
# platform_capabilities.index.json so the dashboard never imports
# esphome.components.wifi (which pulls esp32 -> espidf -> requests ->
# esphome.config onto cold start). Loaded once at module import — a cheap,
# esphome-free JSON read. An empty index degrades to "assume Wi-Fi"
# (fail-open), matching the prior unknown-board behaviour.
_caps = load_platform_capabilities_index()
# ESPHome stores variant tags uppercase (``"ESP32H2"``); normalise to the
# lowercase ``Esp32Variant`` form the wizard compares against.
_ESP32_NO_WIFI_VARIANTS: frozenset[str] = frozenset(v.lower() for v in _caps.esp32_no_wifi_variants)
_RP2040_NO_WIFI_BOARDS: frozenset[str] = frozenset(_caps.rp2040_no_wifi_boards)

_WIFI_FIRST_PLATFORMS: frozenset[str] = frozenset(
    {"esp8266", "bk72xx", "rtl87xx", "ln882x", "libretiny"}
)

# Fallback-hotspot psk alphabet + length, mirroring esphome's wizard.
_AP_PSK_ALPHABET = string.ascii_letters + string.digits
_AP_PSK_LENGTH = 12

# ESPHome's ``cv.ssid`` caps an AP ssid at 32 bytes.
_AP_SSID_MAX_LEN = 32

# Platforms supporting ``captive_portal:`` (esphome's ``cv.only_on``
# allowlist). The fallback is emitted only here; a bare ``ap:`` without
# a portal can't recover credentials, so other platforms get neither.
_CAPTIVE_PORTAL_PLATFORMS: frozenset[str] = frozenset(
    {"esp8266", "esp32", "bk72xx", "ln882x", "rp2040", "rtl87xx"}
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

# Emitted instead of the Wi-Fi block when no ``wifi_ssid`` / ``wifi_password``
# secrets are defined (the user skipped Wi-Fi / uses Ethernet). The board may
# have native Wi-Fi, so unlike ``_NO_NETWORK_TODO_LINES`` this doesn't claim
# otherwise — it just points at adding a network. ``api:`` / ``ota:`` are
# omitted because both require a ``network`` component to validate.
_NO_WIFI_SECRETS_TODO_LINES: tuple[str, ...] = (
    "# No Wi-Fi secrets are set, so this starter has no network yet.",
    "# Add a ``wifi:`` block (with your credentials) or an ``ethernet:`` /",
    "# ``openthread:`` block to suit your board, then add ``api:`` and",
    "# ``ota:`` blocks once the network is ready.",
    "",
)

# Catalog ids of components that satisfy ESPHome's ``network`` dependency.
# When one is supplied through *defaults*, ``generate_device_yaml`` emits
# ``api:`` / ``ota:`` and drops the ``wifi:`` block in its favour. Ethernet
# today; ``openthread`` joins here when Thread boards are wired up. Shared
# with the components controller (``resolve_network_components``) so the
# "what to auto-pull" and "what counts as a network" sets can't diverge.
NETWORK_PROVIDER_COMPONENT_IDS: frozenset[str] = frozenset({"ethernet"})

# Catalog ids of components that give a no-native-Wi-Fi chip a usable Wi-Fi
# radio (an ESP-Hosted co-processor). When one is supplied through
# *defaults*, ``generate_device_yaml`` treats the board as Wi-Fi-capable and
# emits the ``wifi:`` block — ESPHome rejects a bare ``wifi:`` on
# ``NO_WIFI_VARIANTS`` ("WiFi requires component esp32_hosted on ESP32P4").
# Mirrored by ``_WIFI_RADIO_COMPONENT_IDS`` in script/validate_definitions.py;
# keep both in sync.
WIFI_RADIO_PROVIDER_COMPONENT_IDS: frozenset[str] = frozenset({"esp32_hosted"})


def board_provides_network(board: BoardCatalogEntry) -> bool:
    """
    Whether *board* supplies its own network (onboard ``ethernet:``, …).

    True when a featured component (or a bare default-component id) names a
    provider in :data:`NETWORK_PROVIDER_COMPONENT_IDS`. A no-``ssid`` create
    on such a board is wired by default — the generator drops the ``wifi:``
    block — so the wizard skips the Wi-Fi step rather than asking.
    """
    if any(fc.component_id in NETWORK_PROVIDER_COMPONENT_IDS for fc in board.featured_components):
        return True
    return any(dc.id in NETWORK_PROVIDER_COMPONENT_IDS for dc in board.default_components)


def board_has_native_wifi(board: BoardCatalogEntry) -> bool:
    """Whether *board* has built-in Wi-Fi (``connectivity`` hint, else inferred)."""
    connectivity = [c.value for c in board.hardware.connectivity] if board.hardware else []
    return "wifi" in connectivity if connectivity else _infer_native_wifi(board)


def board_requires_wifi(board: BoardCatalogEntry) -> bool:
    """
    Whether the create wizard must collect Wi-Fi for *board*.

    True when Wi-Fi is the board's only built-in network — it has native Wi-Fi
    and provides no onboard non-Wi-Fi network. A generated config needs a
    network (``api`` / ``ota`` / a board's ``web_server`` default all depend on
    one), so Wi-Fi can't be skipped for these boards; the no-network stub would
    fail to validate. Boards that bring their own network
    (:func:`board_provides_network`) skip the Wi-Fi step entirely instead.
    """
    return board_has_native_wifi(board) and not board_provides_network(board)


def _has_native_wifi(
    *, platform: str, board: str | None = None, variant: str | None = None
) -> bool:
    """Return True when *platform* / *board* / *variant* has native Wi-Fi.

    Mirrors ``esphome.components.wifi.has_native_wifi`` from the snapshotted
    platform_capabilities index (esp32 no-Wi-Fi variants + rp2040 no-Wi-Fi
    boards). Allowlist semantics: unknown platforms fail closed, unknown rp2040
    boards fail open (assume Wi-Fi) — both matching upstream.
    """
    if platform == "esp32":
        return not (variant and variant.lower() in _ESP32_NO_WIFI_VARIANTS)
    if platform == "rp2040":
        return board is None or board not in _RP2040_NO_WIFI_BOARDS
    return platform in _WIFI_FIRST_PLATFORMS


# ---------------------------------------------------------------------------
# YAML generation
# ---------------------------------------------------------------------------


def generate_device_yaml(
    name: str,
    friendly_name: str,
    board: BoardCatalogEntry,
    ssid: str,
    psk: str,
    *,
    wifi_secrets_available: bool = True,
    defaults: list[tuple[ComponentCatalogEntry, dict[str, Any]]] | None = None,
) -> str:
    """
    Generate a complete device YAML config from a board definition.

    Produces the base config with platform settings, logging, API, OTA,
    and Wi-Fi — the most common/sane defaults for a new device. When
    *defaults* is non-empty each ``(component, fields)`` pair is
    appended via :func:`merge_component_yaml`, matching the shape
    ``add_component`` would produce on a fresh YAML. When *defaults*
    supplies a network component (``NETWORK_PROVIDER_COMPONENT_IDS``,
    e.g. onboard ``ethernet:``) it takes precedence: the ``wifi:``
    block is dropped and *ssid* / *psk* are ignored.
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

    # ESPHome core. ``name`` arrives already slug-safe (see
    # ``mutations_create``), but ``friendly_name`` is raw user
    # input that may contain ``:``, ``#``, leading indicators, or
    # other YAML metacharacters — route it through the safe-scalar
    # renderer so a label like ``Bedroom #2`` doesn't truncate at
    # the comment marker on round trip.
    lines.append("esphome:")
    lines.append(f"  name: {name}")
    lines.append(f"  friendly_name: {_safe_yaml_scalar(friendly_name)}")
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
            lines.extend(("  framework:", f"    type: {esphome_cfg.framework}"))
            if hardware.flash_size == "32MB":
                # ESPHome refuses ``ota:`` (emitted below whenever a
                # network exists) with 32MB flash unless this opt-in
                # is set.
                lines.extend(("    advanced:", "      enable_idf_experimental_features: true"))
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
    has_wifi = board_has_native_wifi(board)

    # ``api:`` / ``ota:`` both require a ``network`` component
    # (DEPENDENCIES=["network"]), so they're emitted only when one is
    # actually present — an injected provider (onboard ``ethernet:``) or
    # a usable ``wifi:`` block — otherwise validation rejects the config
    # with "Component api requires component network." A network provider
    # in *defaults* takes precedence over Wi-Fi (wired board). Wi-Fi is
    # usable only with a literal ssid (always inlines) or resolvable
    # secrets; a bare ``!secret`` reference with no secrets defined fails
    # validation with "Secret not defined".
    default_ids = {component.id for component, _ in defaults or ()}
    network_provided = not default_ids.isdisjoint(NETWORK_PROVIDER_COMPONENT_IDS)
    # A manifest may claim ``wifi`` connectivity for a no-native-Wi-Fi chip
    # (P4 with an onboard esp32_hosted co-processor). Emitting ``wifi:`` is
    # only valid when the chip itself has the radio or a radio provider
    # ships in *defaults*.
    wifi_radio_present = has_wifi and (
        not default_ids.isdisjoint(WIFI_RADIO_PROVIDER_COMPONENT_IDS) or _infer_native_wifi(board)
    )
    emit_wifi = (
        wifi_radio_present and not network_provided and (bool(ssid) or wifi_secrets_available)
    )
    if network_provided or emit_wifi:
        # Home Assistant API — unique encryption key per device.
        api_key = base64.b64encode(secrets.token_bytes(32)).decode()
        lines.append("api:")
        lines.append("  encryption:")
        lines.append(f'    key: "{api_key}"')
        lines.append("")

        # OTA — same network dependency as ``api:`` above.
        lines.append("ota:")
        lines.append("  - platform: esphome")
        lines.append("")

        if emit_wifi:
            lines.extend(_wifi_block_lines(ssid, psk, friendly_name or name, platform))
    elif wifi_radio_present:
        # Usable Wi-Fi radio but no credentials and no wired network →
        # point the user at adding one (no ``api:`` / ``ota:`` yet).
        lines.extend(_NO_WIFI_SECRETS_TODO_LINES)
    else:
        # No usable Wi-Fi radio (chip has none and no radio provider in
        # *defaults*) → leave a TODO so the user knows what they
        # need to configure before adding ``api:`` / ``ota:``. Both
        # require a ``network`` component to compile, and the right
        # network for these boards depends on the user's setup
        # (``openthread:`` for H2, ``ethernet:`` for P4 with a
        # co-processor, ``esp32_hosted:`` for either with a Wi-Fi
        # daughterboard, etc.). Emitting a placeholder block would
        # bake an arbitrary choice into the generated YAML; a
        # commented-out hint lets the user pick.
        lines.extend(_NO_NETWORK_TODO_LINES)

    return _apply_default_components("\n".join(lines), defaults)


def _wifi_block_lines(ssid: str, psk: str, ap_name: str, platform: str) -> list[str]:
    """
    Build the ``wifi:`` block lines plus the fallback AP / captive portal.

    With *ssid* set, emits explicit credentials; otherwise ``!secret``
    references. *ap_name* / *platform* drive the recovery AP.
    """
    lines = ["wifi:"]
    if ssid:
        # An unquoted SSID like 'Home #2' truncates at the # comment
        # marker; a password starting with an indicator char (*, !, &)
        # fails to parse. Route raw user input through scalar-safe quoting.
        lines.append(f"  ssid: {_safe_yaml_scalar(ssid)}")
        lines.append(f"  password: {_safe_yaml_scalar(psk)}")
    else:
        lines.append("  ssid: !secret wifi_ssid")
        lines.append("  password: !secret wifi_password")
    lines.extend(_fallback_recovery_lines(ap_name, platform))
    return lines


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

    Dispatches through ``_has_native_wifi``, which reads the
    snapshotted platform_capabilities index.
    """
    esphome_cfg = board.esphome
    # ``str(...)`` handles both the production enum (``Platform`` /
    # ``Esp32Variant`` are ``StrEnum``) and bare-string inputs from
    # tests that mock the catalog entry without going through the
    # enum constructors. ``_has_native_wifi`` lowercases the variant
    # itself, so no case normalisation is needed here.
    return _has_native_wifi(
        platform=str(esphome_cfg.platform) if esphome_cfg.platform else "",
        board=esphome_cfg.board,
        variant=str(esphome_cfg.variant) if esphome_cfg.variant else None,
    )


def _apply_default_components(
    yaml_text: str,
    defaults: list[tuple[ComponentCatalogEntry, dict[str, Any]]] | None,
) -> str:
    """Append each ``(component, fields)`` pair to *yaml_text* via merge_component_yaml."""
    if not defaults:
        return yaml_text
    for component, fields in defaults:
        yaml_text = merge_component_yaml(yaml_text, component, fields)
    return yaml_text


def generate_minimal_stub_yaml(
    name: str, friendly_name: str, *, wifi_secrets_available: bool = True
) -> str:
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

    When ``wifi_secrets_available`` is False (no ``wifi_ssid`` /
    ``wifi_password`` in secrets.yaml) the stub omits the Wi-Fi
    ``!secret`` block — and ``api:`` / ``ota:``, which need a
    network — emitting a network TODO instead, so it still
    validates.

    The platform defaults to ``esp32`` with ``board: esp32dev``
    because esp32 is the most common starter target and
    ``esp32dev`` is upstream-canonical (ships in
    ``esphome.const.PLATFORMIO_ESP32_LUT`` and validates without
    the catalog). The leading comment tells the user to replace
    the platform block if their hardware differs, so the silent-
    bind concern is at least called out in the file the user is
    about to edit.
    """
    header = (
        f"esphome:\n  name: {name}\n"
        f"  friendly_name: {_safe_yaml_scalar(friendly_name)}\n\n"
        "# Replace this with your actual platform if you aren't using ESP32.\n"
        "esp32:\n  board: esp32dev\n\n"
        "logger:\n\n"
    )
    if not wifi_secrets_available:
        return header + "\n".join(_NO_WIFI_SECRETS_TODO_LINES)
    api_key = base64.b64encode(secrets.token_bytes(32)).decode()
    recovery = "\n".join(_fallback_recovery_lines(friendly_name or name, "esp32"))
    return (
        header + "api:\n  encryption:\n"
        f'    key: "{api_key}"\n\n'
        "ota:\n  - platform: esphome\n\n"
        "wifi:\n"
        "  ssid: !secret wifi_ssid\n"
        "  password: !secret wifi_password\n"
        f"{recovery}"
    )


def _fallback_recovery_lines(label: str, platform: str) -> list[str]:
    """Fallback hotspot + ``captive_portal:`` recovery lines; bare separator where unsupported."""
    if platform not in _CAPTIVE_PORTAL_PLATFORMS:
        # No captive portal → no fallback, but keep the wifi block's
        # trailing blank-line separator from the unconditional old path.
        return [""]
    psk = "".join(secrets.choice(_AP_PSK_ALPHABET) for _ in range(_AP_PSK_LENGTH))
    return [
        "  ap:",
        f"    ssid: {_safe_yaml_scalar(_fallback_ap_ssid(label))}",
        f'    password: "{psk}"',
        "",
        "captive_portal:",
        "",
    ]


def _fallback_ap_ssid(label: str) -> str:
    """AP ssid ``<label> Fallback Hotspot``; trims <label> so the marker survives the cap."""
    base = label.strip() or "ESPHome"
    suffix = " Fallback Hotspot"
    # Trim the name, not the marker (esphome's wizard drops the whole
    # marker here), so the recovery AP stays identifiable for long names.
    if len(base) + len(suffix) > _AP_SSID_MAX_LEN:
        base = base[: _AP_SSID_MAX_LEN - len(suffix)]
    return f"{base}{suffix}"
