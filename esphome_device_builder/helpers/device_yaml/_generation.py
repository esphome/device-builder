"""Generate complete device YAML and minimal stubs from board definitions."""

from __future__ import annotations

import base64
import secrets
import string
from typing import TYPE_CHECKING, Any

from ..yaml import _safe_yaml_scalar, merge_component_yaml

if TYPE_CHECKING:
    from collections.abc import Callable

    from ...models import BoardCatalogEntry, ComponentCatalogEntry


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
    # ``no-redef`` covers the rebind of the names declared in the
    # ``try`` branch above; ``assignment`` covers the type mismatch
    # between the upstream ``BOARDS`` dict and our ``... | None``
    # annotation. Both diagnostics are intentional — the fallback
    # constants are only consumed when the upstream helper is absent.
    from esphome.components.rp2040.boards import (  # type: ignore[no-redef,assignment]
        BOARDS as _ESPHOME_RP2040_BOARDS,
    )
    from esphome.components.wifi import NO_WIFI_VARIANTS as _ESPHOME_NO_WIFI_VARIANTS

    # ESPHome stores the variant tags in canonical uppercase
    # (``"ESP32H2"``); the wizard compares against the lowercase
    # ``Esp32Variant`` enum value, so normalise once at module
    # load.
    _ESP32_NO_WIFI_VARIANTS = frozenset(v.lower() for v in _ESPHOME_NO_WIFI_VARIANTS)

_FALLBACK_WIFI_FIRST_PLATFORMS: frozenset[str] = frozenset(
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
        # ``_ESPHOME_RP2040_BOARDS`` is typed ``dict[str, dict] | None``
        # because the upstream-helper-available branch leaves it as
        # ``None`` (the upstream ``has_native_wifi`` handles all
        # platforms there). This fallback only runs when that branch
        # didn't take, so the import-from-esphome assignment fired
        # and the value is a real dict — but mypy can't see the
        # runtime correlation. ``None`` here is treated the same as
        # an unknown board: assume Wi-Fi present (matches upstream's
        # default-to-wifi-allowlist semantics).
        if _ESPHOME_RP2040_BOARDS is None:
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
    defaults: list[tuple[ComponentCatalogEntry, dict[str, Any]]] | None = None,
) -> str:
    """
    Generate a complete device YAML config from a board definition.

    Produces the base config with platform settings, logging, API, OTA,
    and Wi-Fi — the most common/sane defaults for a new device. When
    *defaults* is non-empty each ``(component, fields)`` pair is
    appended via :func:`merge_component_yaml`, matching the shape
    ``add_component`` would produce on a fresh YAML.
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
            # An unquoted SSID like 'Home #2' truncates at the # comment
            # marker; a password starting with an indicator char (*, !, &)
            # fails to parse. Route raw user input through scalar-safe quoting.
            lines.append(f"  ssid: {_safe_yaml_scalar(ssid)}")
            lines.append(f"  password: {_safe_yaml_scalar(psk)}")
        else:
            lines.append("  ssid: !secret wifi_ssid")
            lines.append("  password: !secret wifi_password")
        lines.extend(_fallback_recovery_lines(friendly_name or name, platform))
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

    return _apply_default_components("\n".join(lines), defaults)


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
    #
    # Variant is uppercased because the upstream
    # ``esphome.components.wifi.has_native_wifi`` dispatcher
    # compares against ``NO_WIFI_VARIANTS`` literal-equality and
    # that list is built from ``const.VARIANT_*`` (uppercase,
    # ``"ESP32H2"`` / ``"ESP32P4"``) — the dashboard's
    # ``Esp32Variant`` StrEnum carries the lowercase form
    # (``"esp32h2"``), so passing it through verbatim falsely
    # tells the dispatcher H2 / P4 have Wi-Fi and the wizard
    # emits ``wifi:`` / ``api:`` / ``ota:`` blocks the
    # downstream compile then rejects. Our pre-#16300 fallback
    # at ``_fallback_has_native_wifi`` already normalises to
    # lowercase on both sides; the upstream path needs the
    # symmetric uppercase normalisation here.
    return _has_native_wifi(
        platform=str(esphome_cfg.platform) if esphome_cfg.platform else "",
        board=esphome_cfg.board,
        variant=str(esphome_cfg.variant).upper() if esphome_cfg.variant else None,
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
    recovery = "\n".join(_fallback_recovery_lines(friendly_name or name, "esp32"))
    return (
        f"esphome:\n  name: {name}\n"
        f"  friendly_name: {_safe_yaml_scalar(friendly_name)}\n\n"
        "# Replace this with your actual platform if you aren't using ESP32.\n"
        "esp32:\n  board: esp32dev\n\n"
        "logger:\n\n"
        "api:\n  encryption:\n"
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
