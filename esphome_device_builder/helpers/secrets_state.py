"""
Shared ``secrets.yaml`` read / merge / write helpers.

One home for every ``secrets.yaml`` touch so the bundle-import merge,
the Wi-Fi writer (the create wizard and the kebab "Set up Wi-Fi"
action), and the placeholder-state detection don't drift apart. Wi-Fi
credentials are collected per-device in the create wizard, which writes
them here so generated YAML can reference ``!secret wifi_ssid`` instead
of inlining bare credentials.

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
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from esphome import yaml_util
from esphome.const import PLACEHOLDER_WIFI_PASSWORD, PLACEHOLDER_WIFI_SSID
from esphome.core import EsphomeError
from esphome.helpers import write_file as atomic_write_file
from ruamel.yaml import YAML

from ..constants import SECRETS_FILENAME
from ..models import ErrorCode
from .api import CommandError
from .async_ import run_in_executor
from .yaml import load_yaml_fast_then_esphome, write_user_yaml
from .yaml.marks import trim_marks

_LOGGER = logging.getLogger(__name__)

# The exact refs generated YAML uses for the shared Wi-Fi credentials.
WIFI_SSID_SECRET_REF = "!secret wifi_ssid"  # noqa: S105 — secret reference, not a credential
WIFI_PASSWORD_SECRET_REF = "!secret wifi_password"  # noqa: S105 — secret reference, not a credential

SECRETS_FIX_HINT = "Fix it on the Secrets page and try again."

# The trimmed PyYAML duplicate-key error, folded to the two line numbers.
_DUPLICATE_KEY_RE = re.compile(
    r'Duplicate key "(?P<key>[^"]+)" in secrets\.yaml, line (?P<second>\d+), column \d+ '
    r"NOTE: Previous declaration here: in secrets\.yaml, line (?P<first>\d+), column \d+"
)


async def write_secrets_locked[T](lock: asyncio.Lock, fn: Callable[..., T], *args: Any) -> T:
    """
    Run a blocking ``secrets.yaml`` mutator under *lock*, off the event loop.

    Pre-bind keyword args with ``functools.partial``.
    """
    async with lock:
        return await run_in_executor(fn, *args)


def read_secrets_yaml(config_dir: Path) -> dict | None:
    """
    Load ``secrets.yaml`` as a plain dict, or ``None`` on any failure.

    Centralised so every reader (``ConfigController.get_secrets``,
    the create wizard's Wi-Fi check, future MQTT-broker pickup etc.)
    shares one fail-soft contract: missing file ⇒ ``None``,
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
    """``secrets.yaml`` content failed to parse, or a rewrite left a key unresolved."""

    def __init__(self, message: str, *, problem: str | None = None) -> None:
        super().__init__(message)
        # User-facing ``secrets.yaml <problem>`` clause when the file itself parses.
        self.problem = problem


def secrets_unparsable_message(action: str, detail: str, *, problem: str | None = None) -> str:
    """Return the user-facing refusal for *action* when ``secrets.yaml`` is unusable."""
    detail = detail.removesuffix(".")
    if problem is None and (m := _DUPLICATE_KEY_RE.fullmatch(detail)) is not None:
        problem = f'has a duplicate key "{m["key"]}" (lines {m["first"]} and {m["second"]})'
    if problem is None:
        problem = f"doesn't parse: {detail}"
    return f"Can't {action}: secrets.yaml {problem}. {SECRETS_FIX_HINT}"


def validate_secrets_content(content: str, path: Path) -> dict:
    """
    Return *content* parsed as a ``secrets.yaml`` mapping, or raise ``SecretsContentError``.

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
        raise SecretsContentError(trim_marks(str(err))) from err
    except Exception as err:
        # A self-referential ``!secret`` inside secrets.yaml recurses in the
        # loader (it crashes the real reader too); reject instead of 500ing.
        # Nothing derived from the file: the loader's message can quote a secret.
        _LOGGER.warning("secrets.yaml could not be parsed by the esphome loader; rejecting")
        raise SecretsContentError(trim_marks(f"secrets.yaml could not be parsed: {err}")) from err
    if data is not None and not isinstance(data, dict):
        raise SecretsContentError("secrets.yaml must be a top-level mapping of name: value entries")
    return data or {}


def wifi_secrets_defined(secrets: dict | None) -> bool:
    """
    Return True when a generated ``!secret`` Wi-Fi block would validate.

    ``wifi_ssid`` must be a non-empty string (ESPHome's ``cv.ssid`` rejects an
    empty SSID) and ``wifi_password`` a string — empty is allowed (open
    networks), but ``null`` / non-string is rejected by ``cv.string_strict``, so
    those count as undefined.
    """
    if secrets is None:
        return False
    ssid = secrets.get("wifi_ssid")
    return (
        isinstance(ssid, str)
        and ssid.strip() != ""
        and isinstance(secrets.get("wifi_password"), str)
    )


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
    write_user_yaml(dest, existing_text + separator + buf.getvalue())


# ``key: value`` line; quoted values may contain ``#`` and the key may be
# quoted. Only indent / key / trailing comment survive a rewrite.
_SECRET_LINE_RE = re.compile(
    r"""^(?P<indent>\s*)(?P<q>["']?)(?P<key>[a-zA-Z_]\w*)(?P=q)\s*:"""
    r"""\s*(?:"(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|.*?)"""
    r"""(?P<comment>\s+#.*)?\s*$"""
)

# A settable secret key. Anchored to the same shape ``_SECRET_LINE_RE``'s
# ``key`` group matches, so "what we accept" tracks "what the line-based setter can
# find and replace" — a key the setter could never locate must not be written.
_SECRET_KEY_RE = re.compile(r"[a-zA-Z_]\w*\Z")


def is_valid_secret_key(key: str) -> bool:
    """Whether *key* is a writable ``!secret`` name (identifier-shaped)."""
    return isinstance(key, str) and _SECRET_KEY_RE.match(key) is not None


# Cap inputs at the same length ESPHome's own validators enforce —
# ``cv.ssid`` (32 chars) and the WPA password validator (64 chars).
MAX_SSID_LEN = 32
MAX_WIFI_PASSWORD_LEN = 64


def validate_wifi_credentials(ssid: str, password: str) -> None:
    """
    Raise ``SecretsContentError`` unless *ssid* / *password* are writable.

    Enforces ESPHome's SSID (32) / WPA password (64) length caps, rejects an
    empty / all-whitespace SSID, and bans control characters (except TAB) the
    single-line ``secrets.yaml`` rewrite can't carry. Shared by
    ``config/set_wifi_credentials`` and ``devices/create`` so both surfaces
    persist the same valid values. SSID whitespace is otherwise preserved —
    a network may legitimately be named with leading / trailing spaces.
    """
    if not isinstance(ssid, str):
        raise SecretsContentError("SSID must be a string.")
    if not isinstance(password, str):
        raise SecretsContentError("Password must be a string.")
    if not ssid.strip():
        raise SecretsContentError("SSID can't be empty.")
    if len(ssid) > MAX_SSID_LEN:
        raise SecretsContentError(f"SSID can't be longer than {MAX_SSID_LEN} characters.")
    if len(password) > MAX_WIFI_PASSWORD_LEN:
        raise SecretsContentError(
            f"Password can't be longer than {MAX_WIFI_PASSWORD_LEN} characters."
        )
    for label, value in (("SSID", ssid), ("Password", password)):
        if any(c != "\t" and (ord(c) < 0x20 or ord(c) == 0x7F) for c in value):
            raise SecretsContentError(f"{label} can't contain control characters.")


def migrate_placeholder_wifi_secrets(config_dir: Path) -> None:
    """
    Drop seeded placeholder Wi-Fi secrets from an existing ``secrets.yaml``.

    Older builds bootstrapped ``wifi_ssid`` / ``wifi_password`` placeholders.
    Wi-Fi is now collected per-device, so a leftover placeholder would make a
    no-``ssid`` create emit ``!secret wifi_ssid`` pointing at the placeholder
    (the device compiles but never joins). Remove each key only while it still
    holds the exact placeholder, leaving real values and other secrets intact.
    Idempotent; a no-op on fresh installs (no placeholders seeded).
    """
    secrets_path = config_dir / SECRETS_FILENAME
    if not secrets_path.exists():
        return
    data = read_secrets_yaml(config_dir)
    if not data:
        return
    drop = {
        key
        for key, placeholder in (
            ("wifi_ssid", PLACEHOLDER_WIFI_SSID),
            ("wifi_password", PLACEHOLDER_WIFI_PASSWORD),
        )
        if data.get(key) == placeholder
    }
    if not drop:
        return
    original = secrets_path.read_text(encoding="utf-8")
    updated = "\n".join(
        line
        for line in original.split("\n")
        if not ((m := _SECRET_LINE_RE.match(line)) and not m["indent"] and m["key"] in drop)
    )
    if updated == original:
        return
    try:
        validate_secrets_content(updated, secrets_path)
    except SecretsContentError:
        _LOGGER.warning(
            "Leaving the placeholder Wi-Fi secrets in place; the rewrite wouldn't parse"
        )
        return
    write_user_yaml(secrets_path, updated)


async def set_wifi_secrets(
    write_locked: Callable[..., Awaitable[Any]], config_dir: Path, ssid: str, password: str
) -> None:
    """Validate and store Wi-Fi credentials via *write_locked*; refusals raise ``CommandError``."""
    try:
        validate_wifi_credentials(ssid, password)
    except SecretsContentError as err:
        raise CommandError(ErrorCode.INVALID_ARGS, str(err)) from err
    try:
        await write_locked(write_wifi_secrets, config_dir, ssid, password)
    except SecretsContentError as err:
        raise CommandError(
            ErrorCode.INVALID_ARGS,
            secrets_unparsable_message("save Wi-Fi credentials", str(err), problem=err.problem),
        ) from err


def write_wifi_secrets(config_dir: Path, ssid: str, password: str) -> None:
    """
    Set ``wifi_ssid`` / ``wifi_password`` in ``secrets.yaml`` in place.

    Raises ``SecretsContentError`` (nothing written) when the result wouldn't
    parse or doesn't resolve either key to the new value.
    """
    secrets_path = config_dir / SECRETS_FILENAME
    original = secrets_path.read_text(encoding="utf-8") if secrets_path.exists() else ""
    resolved = _resolved_keys(original, secrets_path)
    updated = original
    for key, value in (("wifi_ssid", ssid), ("wifi_password", password)):
        defined = key in resolved if isinstance(resolved, set) else None
        updated = _replace_or_append_secret(updated, key, value, defined=defined)
    _write_validated_secrets(
        secrets_path,
        updated,
        {"wifi_ssid": ssid, "wifi_password": password},
        original_error=resolved if isinstance(resolved, SecretsContentError) else None,
    )


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
    resolved = _resolved_keys(original, secrets_path)
    if isinstance(resolved, SecretsContentError):
        # "Already exists" can't be answered from a file esphome won't read.
        if not overwrite:
            raise resolved
        existed, defined = _line_defines_key(original, key), None
    else:
        existed = defined = key in resolved
    if existed and not overwrite:
        return False
    updated = _replace_or_append_secret(original, key, value, defined=defined)
    _write_validated_secrets(
        secrets_path,
        updated,
        {key: value},
        original_error=resolved if isinstance(resolved, SecretsContentError) else None,
    )
    return not existed


def _write_validated_secrets(
    secrets_path: Path,
    content: str,
    expected: dict[str, str],
    *,
    original_error: SecretsContentError | None = None,
) -> None:
    """Write *content* to *secrets_path* once it parses and resolves every *expected* key."""
    try:
        data = validate_secrets_content(content, secrets_path)
    except SecretsContentError:
        # A collapse shifts lines; report the on-disk file's own error when it has one.
        if original_error is not None:
            raise original_error from None
        raise
    for key, value in expected.items():
        if data.get(key) != value:
            raise SecretsContentError(
                f'"{key}" is defined where the dashboard can\'t rewrite it',
                problem=f'defines "{key}" where the dashboard can\'t rewrite it',
            )
    write_user_yaml(secrets_path, content)


def _resolved_keys(content: str, path: Path) -> set[str] | SecretsContentError:
    """Return the secret names *content* resolves, or the error when it won't parse."""
    try:
        return set(validate_secrets_content(content, path))
    except SecretsContentError as err:
        return err


def _line_defines_key(content: str, key: str) -> bool:
    """Whether any line of *content* is a ``key:`` line (the setter's match rule)."""
    return any(
        (m := _SECRET_LINE_RE.match(line)) is not None and m["key"] == key
        for line in content.split("\n")
    )


def _replace_or_append_secret(
    content: str, key: str, value: str, *, defined: bool | None = None
) -> str:
    """
    Set ``key`` to ``value`` in *content* in place, or append ``key: "value"``.

    Rewrites the top-level match and drops later top-level duplicates; with
    none, rewrites the first nested match unless *defined* says the parsed
    file doesn't resolve *key*, in which case a top-level line is appended.
    """
    encoded = _quote_yaml_string(value)
    lines = content.split("\n")
    matches = [
        (i, m)
        for i, line in enumerate(lines)
        if (m := _SECRET_LINE_RE.match(line)) is not None and m["key"] == key
    ]
    top_level = [(i, m) for i, m in matches if not m["indent"]]
    target: tuple[int, re.Match[str]] | None = None
    drop: set[int] = set()
    if top_level:
        target, drop = top_level[0], {i for i, _ in top_level[1:]}
    elif matches and defined is not False:
        target = matches[0]
    if target is not None:
        index, m = target
        lines[index] = f"{m['indent']}{key}: {encoded}{m['comment'] or ''}"
        return "\n".join(line for i, line in enumerate(lines) if i not in drop)
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
