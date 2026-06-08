"""
Shared ``secrets.yaml`` read / merge / write helpers.

One home for every ``secrets.yaml`` touch so the bundle-import merge,
the onboarding wifi writer, and the placeholder-state detection don't
drift apart. The dashboard's first-run bootstrap writes deterministic
placeholder strings into ``secrets.yaml`` so ``!secret wifi_ssid``
references in generated YAML resolve cleanly through ESPHome's
validator; the same constants here detect whether the user has
supplied real values yet.

Two mutation shapes live here on purpose:
``_replace_or_append_secret`` / ``write_wifi_secrets`` *set* specific
keys (line-based, preserving inline comments), while
``merge_secrets_file`` *unions in* a whole incoming mapping keeping
existing values on conflict (ruamel round-trip).
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from esphome import yaml_util
from esphome.core import EsphomeError
from esphome.helpers import write_file as atomic_write_file
from ruamel.yaml import YAML

from ..constants import SECRETS_FILENAME
from .yaml import load_yaml_fast_then_esphome

_LOGGER = logging.getLogger(__name__)


async def write_secrets_locked[T](lock: asyncio.Lock, fn: Callable[..., T], *args: Any) -> T:
    """
    Run a blocking ``secrets.yaml`` mutator under *lock*, off the event loop.

    Pre-bind keyword args with ``functools.partial``.
    """
    loop = asyncio.get_running_loop()
    async with lock:
        return await loop.run_in_executor(None, fn, *args)


# Bootstrap placeholder strings. Upstream now exports these from
# ``esphome.const``; fall back to local literals on older releases
# that predate the promotion.
try:
    from esphome.const import (
        PLACEHOLDER_WIFI_PASSWORD,
        PLACEHOLDER_WIFI_SSID,
    )
except ImportError:
    PLACEHOLDER_WIFI_SSID = "REPLACE_WITH_YOUR_WIFI_NETWORK"
    PLACEHOLDER_WIFI_PASSWORD = "REPLACE_WITH_YOUR_WIFI_PASSWORD"  # noqa: S105

# Values that count as "not user-configured" for ``wifi_ssid``:
# missing key, empty string, or the bootstrap placeholder. Stored
# as a frozenset so a future placeholder rotation just appends
# the old value here for backward compatibility.
_UNCONFIGURED_WIFI_SSID_VALUES: frozenset[str] = frozenset({"", PLACEHOLDER_WIFI_SSID})


def read_secrets_yaml(config_dir: Path) -> dict | None:
    """
    Load ``secrets.yaml`` as a plain dict, or ``None`` on any failure.

    Centralised so every reader (``ConfigController.get_secrets``,
    ``OnboardingController.get_state``, future MQTT-broker pickup
    etc.) shares one fail-soft contract: missing file ⇒ ``None``,
    parse error ⇒ ``None``, non-dict top-level (``secrets.yaml``
    that's a list or scalar — invalid but possible) ⇒ ``None``.

    ``yaml_util.load_yaml`` expects a ``Path``, not a ``str`` — the
    type signature pins this so a string slip from a caller fails
    at type-check time instead of as an ``AttributeError`` slipping
    past the narrow ``EsphomeError`` catch below.
    """
    secrets_path = config_dir / SECRETS_FILENAME
    if not secrets_path.exists():
        return None
    try:
        data = yaml_util.load_yaml(secrets_path)
    except EsphomeError:
        return None
    return data if isinstance(data, dict) else None


class SecretsContentError(ValueError):
    """``secrets.yaml`` content failed to parse or isn't a top-level mapping."""


def validate_secrets_content(content: str, path: Path) -> None:
    """
    Raise ``SecretsContentError`` unless *content* is a valid ``secrets.yaml``.

    Parsed through ESPHome's own loader, keyed on the real on-disk *path*,
    so ``!include`` / ``!secret`` / merge keys resolve against the config
    dir exactly as the saved file would be read, and duplicate keys the
    plain ``SafeLoader`` silently accepts are rejected. A non-mapping top
    level (list / scalar) is rejected. Any loader failure is surfaced as a
    rejection rather than a 500; the message carries the line/column.
    """
    try:
        data = yaml_util.parse_yaml(path, io.StringIO(content))
    except EsphomeError as err:
        raise SecretsContentError(str(err)) from err
    except Exception as err:
        # A self-referential ``!secret`` inside secrets.yaml recurses in the
        # loader (it crashes the real reader too); reject instead of 500ing.
        raise SecretsContentError(f"secrets.yaml could not be parsed: {err}") from err
    if data is not None and not isinstance(data, dict):
        raise SecretsContentError("secrets.yaml must be a top-level mapping of name: value entries")


def is_wifi_unconfigured(secrets: dict | None) -> bool:
    """
    Return True when ``secrets.yaml``'s ``wifi_ssid`` is missing / empty / placeholder.

    Only the SSID is checked — ESPHome's ``cv.ssid`` validator
    rejects empty strings ("SSID can't be empty.") while
    ``cv.string_strict`` on the password accepts ``""`` (open
    networks are valid). So the SSID is the canonical "wifi
    is configured" signal; matching on it alone keeps the
    state-check minimal.

    Boundary cases:

    - Missing file / empty dict / missing key → unconfigured.
    - Non-string value (e.g. ``wifi_ssid: 42`` — quotes stripped
      by accident) → unconfigured. ESPHome's compile-time
      validator would reject it later anyway, and clearing
      onboarding here would mask a real broken-config state from
      the user.
    """
    if not secrets:
        return True
    val = secrets.get("wifi_ssid")
    if not isinstance(val, str):
        return True
    return val.strip() in _UNCONFIGURED_WIFI_SSID_VALUES


def merge_secrets_file(src: Path, dest: Path) -> None:
    """
    Merge *src* secrets into *dest*, adding only keys *dest* lacks.

    Key sets are read with the tolerant loader so an HA-shared
    ``secrets.yaml`` (``<<: !include`` / ``!secret`` tags) still merges
    rather than silently no-op'ing on the unknown tag (#1220). New keys
    are appended to *dest*'s text so its existing tags and comments
    survive untouched; existing values are never changed. *dest* is left
    untouched (with a warning) when either side can't be read as a
    mapping, so a malformed file never silently drops the bundle's keys.
    """
    if not dest.exists():
        atomic_write_file(dest, src.read_bytes())
        return
    try:
        existing = load_yaml_fast_then_esphome(dest) or {}
        incoming = load_yaml_fast_then_esphome(src) or {}
    except (EsphomeError, OSError, UnicodeDecodeError) as err:
        _LOGGER.warning("Couldn't read secrets for merge (%s); left %s untouched", err, dest)
        return
    if not isinstance(existing, dict) or not isinstance(incoming, dict):
        _LOGGER.warning("secrets.yaml isn't a mapping; left %s untouched", dest)
        return
    absent = {key: value for key, value in incoming.items() if key not in existing}
    if not absent:
        return
    # Append the new keys; never reparse/redump the existing file, so its
    # tags and comments are preserved byte-for-byte.
    buf = io.StringIO()
    YAML().dump(absent, buf)
    existing_text = dest.read_text("utf-8")
    separator = "" if not existing_text or existing_text.endswith("\n") else "\n"
    atomic_write_file(dest, existing_text + separator + buf.getvalue())


# ``key: value`` line. Captures: 1=indent, 2=key, 3=trailing
# ``  # comment`` (with at least one space before the ``#``).
# Permissive on value shape so we match both ``wifi_ssid: ""``
# and bare ``wifi_ssid:`` — the value itself is discarded on
# rewrite, only indent / key / trailing comment carry over.
#
# Known limitation: a ``#`` *inside a quoted value* preceded by
# whitespace (e.g. ``wifi_ssid: "foo # bar"``) is mis-parsed as
# a trailing comment. The rewrite still produces valid YAML
# because the new value is re-quoted, but the spurious tail is
# preserved as a comment. See the dedicated regression test in
# ``tests/test_onboarding_controller.py``. Realistic impact is
# low — ``#`` in SSIDs is uncommon and the user's hand-edit has
# to land before they re-run the wizard.
_SECRET_LINE_RE = re.compile(r"^(\s*)([a-zA-Z_]\w*)\s*:[^#\n]*?(\s+#.*)?$")

# A settable secret key. Anchored to the same shape ``_SECRET_LINE_RE``'s key
# group matches, so "what we accept" tracks "what the line-based setter can
# find and replace" — a key the setter could never locate must not be written.
_SECRET_KEY_RE = re.compile(r"[a-zA-Z_]\w*\Z")


def is_valid_secret_key(key: str) -> bool:
    """Whether *key* is a writable ``!secret`` name (identifier-shaped)."""
    return isinstance(key, str) and _SECRET_KEY_RE.match(key) is not None


def write_wifi_secrets(config_dir: Path, ssid: str, password: str) -> None:
    """
    Update ``wifi_ssid`` and ``wifi_password`` in ``secrets.yaml`` in place.

    Line-based rewrite preserves comments and any other secrets the
    user has added. Falls back to creating the file with just the
    two keys if it doesn't exist (the bootstrap should have created
    it on startup, but a user who deleted it shouldn't be stuck
    here).
    """
    secrets_path = config_dir / SECRETS_FILENAME
    original = secrets_path.read_text(encoding="utf-8") if secrets_path.exists() else ""

    updated = _replace_or_append_secret(
        _replace_or_append_secret(original, "wifi_ssid", ssid),
        "wifi_password",
        password,
    )
    atomic_write_file(secrets_path, updated)


def write_secret(config_dir: Path, key: str, value: str, *, overwrite: bool = True) -> bool:
    """
    Set one *key* in ``secrets.yaml`` in place; return True when newly created.

    With ``overwrite=False`` an existing key is left untouched (the
    create-if-absent path). The read-modify-write is **not** locked here —
    the caller must hold the shared secrets write lock so concurrent
    single-key sets don't lose each other's update.
    """
    secrets_path = config_dir / SECRETS_FILENAME
    original = secrets_path.read_text(encoding="utf-8") if secrets_path.exists() else ""
    existed = _has_secret_key(original, key)
    if existed and not overwrite:
        return False
    updated = _replace_or_append_secret(original, key, value)
    validate_secrets_content(updated, secrets_path)
    atomic_write_file(secrets_path, updated)
    return not existed


def _has_secret_key(content: str, key: str) -> bool:
    """Whether *content* already defines top-level *key* (setter's match rule)."""
    return any(
        (m := _SECRET_LINE_RE.match(line)) is not None and m.group(2) == key
        for line in content.split("\n")
    )


def _replace_or_append_secret(content: str, key: str, value: str) -> str:
    """
    Set ``key`` to ``value`` in YAML *content*, in place.

    Replaces the value on **every** line whose key matches — a
    duplicated key in ``secrets.yaml`` is malformed (PyYAML keeps
    only the last on read), but writing only the first match
    would leave the stale duplicate as the live value and
    onboarding would stay PENDING after a "successful" save. Any
    inline ``# comment`` trailing the matched line is preserved
    so a power-user with ``wifi_ssid: home  # Apt 4B router``
    keeps the annotation. If no line matches, appends
    ``key: "value"`` at the end with a trailing newline.
    """
    encoded = _quote_yaml_string(value)
    lines = content.split("\n")
    matched = False
    for i, line in enumerate(lines):
        m = _SECRET_LINE_RE.match(line)
        if m and m.group(2) == key:
            trailing_comment = m.group(3) or ""
            lines[i] = f"{m.group(1)}{key}: {encoded}{trailing_comment}"
            matched = True
    if matched:
        return "\n".join(lines)
    # Append. Empty input gets the line on its own (no leading
    # blank); any other input gets a single ``\n`` separator if it
    # doesn't already end with one.
    if not content:
        return f"{key}: {encoded}\n"
    if not content.endswith("\n"):
        content = content + "\n"
    return f"{content}{key}: {encoded}\n"


_YAML_DQ_ESCAPES: dict[str, str] = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\t": "\\t",
    "\r": "\\r",
}


def _quote_yaml_string(value: str) -> str:
    r"""
    Quote *value* as a YAML double-quoted scalar that round-trips exactly.

    Escapes ``\`` and ``"``, plus the control characters a literal would
    otherwise fold or mangle on read — newline/tab/CR get their named
    escapes and any other C0/DEL byte becomes ``\xHH``. Without this a
    secret containing a real newline writes a multi-line scalar that
    folds back to a space on read (silent round-trip corruption).
    """
    out: list[str] = []
    for ch in value:
        if ch in _YAML_DQ_ESCAPES:
            out.append(_YAML_DQ_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\x{ord(ch):02x}")
        else:
            out.append(ch)
    return f'"{"".join(out)}"'
