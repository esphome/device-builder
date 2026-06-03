"""Parse and inspect device YAML: platform, esphome-meta, flags, substitutions."""

from __future__ import annotations

import re
from typing import NamedTuple

from esphome import const
from esphome.const import CONF_PACKAGES


class EsphomeMeta(NamedTuple):
    """The user-facing ``esphome:`` meta fields; ``None`` for any the YAML omits."""

    name: str | None
    friendly_name: str | None
    comment: str | None
    area: str | None


_PLATFORM_KEYS = frozenset({"esp32", "esp8266", "rp2040", "bk72xx", "rtl87xx", "ln882x", "nrf52"})


_SUBSTITUTION_RE = re.compile(r"\$(\{[a-zA-Z0-9_]*\}|[a-zA-Z0-9_]+)")

# "Looks like an unresolved substitution token" — any ``${...}`` (incl. the
# nested jinja ``${device.area}`` form ``_SUBSTITUTION_RE`` deliberately
# won't match) or ``$identifier``. Excludes a literal ``$`` followed by a
# digit / space / punctuation (e.g. ``"Replaces a $40 sensor"``) so a
# fully-resolved edit carrying a literal ``$`` isn't mistaken for one.
_UNRESOLVED_SUBSTITUTION_RE = re.compile(r"\$\{|\$[a-zA-Z_]")

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
        top_key = _match_top_level_key(line)
        if top_key is not None:
            if top_key in _PLATFORM_KEYS:
                platform = top_key
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


def detect_platform_from_yaml(yaml_content: str, resolved_config: dict | None) -> str:
    """
    Find a config's platform key from its raw text and merged config.

    Cheap line-scan of *yaml_content* first (survives mid-edit
    drafts); falls back to *resolved_config* only on a raw-scan
    miss when the text has a ``packages:`` block, since a packaged
    ``esp32:`` key only appears post-merge. Empty string when
    neither turns one up.
    """
    try:
        platform, _, _ = parse_platform_from_yaml(yaml_content)
    except Exception:  # noqa: BLE001 — future-proof against parse_platform_from_yaml gaining a throw shape
        platform = ""
    if platform:
        return platform
    if not yaml_has_top_level_block(yaml_content, CONF_PACKAGES):
        # No ``packages:`` block in the raw text → the merge can't
        # surface a platform key that wasn't already there.
        return ""
    if isinstance(resolved_config, dict):
        for key in resolved_config:
            # ``key`` is ``Any`` (dict came from ``yaml_util.load_yaml``
            # which is untyped); the runtime contract is "platform keys
            # are always strings", so narrow with ``isinstance`` before
            # returning rather than blind-returning ``Any`` and tripping
            # ``no-any-return``.
            if isinstance(key, str) and key in _PLATFORM_KEYS:
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
    return any(_match_top_level_key(line) == key for line in yaml_content.splitlines())


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


def parse_esphome_meta(
    yaml_content: str,
    extra_substitutions: dict[str, str] | None = None,
) -> EsphomeMeta:
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

    *extra_substitutions* is an optional fallback map of substitutions
    contributed by ``packages:`` / ``!include`` blocks — typically the
    ``substitutions`` key off the resolved config returned by
    :func:`load_device_yaml`. The file's own ``substitutions:`` block
    still wins on key conflicts, mirroring esphome's package merge
    precedence (local config overrides package contributions).
    """
    meta: dict[str, str | None] = dict.fromkeys(_ESPHOME_META_FIELDS)
    substitutions: dict[str, str] = {}
    current_block: str | None = None
    esphome_child_indent: int | None = None
    area_block_indent: int | None = None

    for line in yaml_content.splitlines():
        top_key = _match_top_level_key(line)
        if top_key is not None:
            current_block = top_key if top_key in ("esphome", "substitutions") else None
            esphome_child_indent = None
            area_block_indent = None
            continue
        if current_block is None:
            continue
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        if current_block == "esphome":
            indent = len(line) - len(line.lstrip())
            esphome_child_indent, area_block_indent = _consume_esphome_line(
                stripped, indent, meta, esphome_child_indent, area_block_indent
            )
        else:  # current_block == "substitutions"
            sub_key, sep, sub_raw = stripped.partition(":")
            if sep:
                substitutions[sub_key.strip()] = _parse_inline_value(sub_raw)

    # Merge extras under the file-local substitutions so a key defined
    # both in the file and in a package keeps the local value — the
    # same precedence esphome applies during ``do_packages_pass``.
    merged = {**extra_substitutions, **substitutions} if extra_substitutions else substitutions

    if merged:
        for field in _ESPHOME_META_FIELDS:
            meta[field] = _resolve_substitutions(meta[field], merged)

    return EsphomeMeta(meta["name"], meta["friendly_name"], meta["comment"], meta["area"])


def extract_esphome_meta_from_config(
    config: dict | None,
    extra_substitutions: dict[str, str] | None = None,
) -> EsphomeMeta:
    """
    Read ``(name, friendly_name, comment, area)`` off a resolved config's ``esphome:`` block.

    ``None`` for any field absent. ``$var`` / ``${var}`` resolve against
    *extra_substitutions*; tokens the resolver can't expand are left intact.
    """
    if not isinstance(config, dict):
        return EsphomeMeta(None, None, None, None)
    esphome = config.get(const.CONF_ESPHOME)
    if not isinstance(esphome, dict):
        return EsphomeMeta(None, None, None, None)
    subs = extra_substitutions or {}

    def _resolved(value: str | None) -> str | None:
        return _resolve_substitutions(value, subs)

    return EsphomeMeta(
        _resolved(_str_or_none(esphome.get(const.CONF_NAME))),
        _resolved(_str_or_none(esphome.get(const.CONF_FRIENDLY_NAME))),
        _resolved(_str_or_none(esphome.get(const.CONF_COMMENT))),
        _resolved(_area_name_from_config(esphome.get(const.CONF_AREA))),
    )


def _str_or_none(value: object) -> str | None:
    """Return *value* when it's a string, else ``None``."""
    return value if isinstance(value, str) else None


def _area_name_from_config(value: object) -> str | None:
    """
    Extract the area label from a config ``area`` value.

    String form is the label itself; the ``{id, name}`` mapping form
    surfaces its ``name``. ``None`` for any other shape.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _str_or_none(value.get(const.CONF_NAME))
    return None


def _pick_meta(
    yaml_value: str | None,
    config_value: str | None,
    storage_value: str | None,
) -> str | None:
    """
    Pick the value to display: first fully-resolved of yaml → config → storage.

    A value still holding a substitution token (``${device.area}``) is
    skipped for the next source. The final fallback surfaces only the
    raw-text token or ``None`` — never an unresolved config token.
    """
    for value in (yaml_value, config_value):
        if value is not None and not _UNRESOLVED_SUBSTITUTION_RE.search(value):
            return value
    if storage_value:
        return storage_value
    return yaml_value


def _match_top_level_key(line: str) -> str | None:
    """
    Return the key for a top-level ``key:`` line, or ``None``.

    Skips blank lines, indented lines, comments, and lines without
    a colon, so a top-level ``# Comment: ...`` doesn't masquerade as
    a real YAML key and prematurely close the block being scanned.
    """
    if not line or line[0].isspace():
        return None
    stripped = line.strip()
    if stripped.startswith("#") or ":" not in stripped:
        return None
    return stripped.split(":", 1)[0].strip()


def _parse_inline_value(raw: str) -> str:
    """
    Clean a raw YAML scalar value.

    Strips an inline ``# comment`` and matching surrounding quotes.
    """
    value = raw.strip()
    if "#" in value and not value.startswith(('"', "'")):
        value = value.split("#", 1)[0].rstrip()
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        value = value[1:-1]
    return value


_FLOW_AREA_NAME_RE = re.compile(r"""\bname\s*:\s*("[^"]*"|'[^']*'|[^,}]+)""")

_ESPHOME_META_FIELDS = ("name", "friendly_name", "comment", "area")


def _parse_flow_area_name(raw: str) -> str | None:
    """Extract ``name`` from a flow-form ``area`` mapping; ``None`` when absent."""
    match = _FLOW_AREA_NAME_RE.search(raw)
    if match is None:
        return None
    return _parse_inline_value(match.group(1))


def _capture_area_from_remainder(remainder: str) -> tuple[str | None, bool]:
    """
    Parse the inline part of ``area:`` into ``(value, enter_block)``.

    ``enter_block=True`` defers to the nested sub-block; ``name``
    arrives on a later line. ``False`` means *value* is already
    final (a flow-form mapping or a plain inline scalar).
    """
    stripped_remainder = remainder.strip()
    if not stripped_remainder:
        return None, True
    if stripped_remainder.startswith("{"):
        return _parse_flow_area_name(stripped_remainder), False
    return _parse_inline_value(remainder), False


def _consume_esphome_line(
    stripped: str,
    indent: int,
    meta: dict[str, str | None],
    esphome_child_indent: int | None,
    area_block_indent: int | None,
) -> tuple[int | None, int | None]:
    """
    Consume one non-blank, non-comment line inside the ``esphome:`` block.

    Mutates *meta* with any matched field and returns the updated
    ``(esphome_child_indent, area_block_indent)`` carry so the caller's
    loop can track the sub-block boundary across lines.
    """
    if area_block_indent is not None:
        if indent > area_block_indent:
            # Inside a nested ``area:`` block — only ``name:`` carries
            # the label the dashboard surfaces. Suppressing the
            # top-level prefix matcher here also keeps the nested
            # ``name:`` from clobbering ``esphome.name``.
            if stripped.startswith("name:"):
                meta["area"] = _parse_inline_value(stripped[len("name:") :])
            return esphome_child_indent, area_block_indent
        area_block_indent = None
    if esphome_child_indent is None:
        esphome_child_indent = indent
    if indent != esphome_child_indent:
        return esphome_child_indent, area_block_indent
    for field in _ESPHOME_META_FIELDS:
        prefix = f"{field}:"
        if not stripped.startswith(prefix):
            continue
        remainder = stripped[len(prefix) :]
        if field == "area":
            captured, enter_block = _capture_area_from_remainder(remainder)
            if enter_block:
                area_block_indent = indent
            else:
                meta["area"] = captured
        else:
            meta[field] = _parse_inline_value(remainder)
        break
    return esphome_child_indent, area_block_indent


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


def _extract_resolved_substitutions(config: dict | None) -> dict[str, str]:
    """
    Pull a string-only ``substitutions:`` map off a resolved config.

    Skips entries whose value isn't a string — substitution values
    are always strings in valid ESPHome configs, and the meta reader
    only knows how to interpolate strings. Returns ``{}`` when the
    config is ``None``, has no ``substitutions:`` block, or its
    block isn't a mapping.
    """
    if not isinstance(config, dict):
        return {}
    block = config.get(const.CONF_SUBSTITUTIONS)
    if not isinstance(block, dict):
        return {}
    return {k: v for k, v in block.items() if isinstance(k, str) and isinstance(v, str)}
