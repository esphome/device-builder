"""Generate / rewrite the ESPHome ``api.encryption.key`` literal."""

from __future__ import annotations

import base64
import re
import secrets
from collections.abc import Callable
from enum import Enum, auto

from .ota_encryption import (
    _has_encryption_block,
    drop_ota_encryption_key,
    drop_ota_encryption_old_key,
    read_ota_encryption_key,
    read_ota_encryption_keys,
    read_ota_encryption_old_key,
    rewrite_ota_encryption_key,
    set_ota_encryption_old_key,
)
from .scalar import (
    ESPHOME_YAML_INDENT,
    YamlUpsertNotSupportedError,
    _quote,
    _strip_yaml_quotes,
    is_indirected_scalar,
    plain_literal_value,
    read_yaml_scalar,
    rewrite_yaml_scalar,
)
from .scan import key_line_res, normalize_trailing_newline, trim_trailing_blanks
from .top_block import _insert_top_block_after, _locate_top_block

# esphome's own dump order: the api block follows whichever of these the file has.
_API_BLOCK_ANCHORS = ("esphome", "packages", "substitutions")

API_ENCRYPTION_KEY_PATH = ("api", "encryption", "key")


class OldKeyOutcome(Enum):
    """What became of the api key a rotation replaced, as the OTA ``old_key``."""

    NOT_NEEDED = auto()
    RECORDED = auto()
    NOT_RECORDED = auto()
    KEPT_EXISTING = auto()
    KEPT_INDIRECTED = auto()
    NO_ENCRYPTION_BLOCK = auto()


def api_key_settled(yaml_text: str, key: str) -> bool:
    """Whether the api key is the literal *key* and any explicit OTA key matches or is indirect."""
    return _literal_key_matches(read_yaml_scalar(yaml_text, API_ENCRYPTION_KEY_PATH), key) and (
        _ota_key_matches(yaml_text, key)
    )


def generate_api_encryption_key() -> str:
    """Return a fresh 32-byte ESPHome API encryption key, base64-encoded."""
    return base64.b64encode(secrets.token_bytes(32)).decode()


def rewrite_api_encryption_key(yaml_text: str, new_key: str) -> str:
    """Replace the literal ``api.encryption.key`` and reconcile the OTA block beneath it."""
    return _rewrite(yaml_text, new_key, None, record=False, replace=False)[0]


def upsert_api_encryption_key(yaml_text: str, new_key: str) -> str:
    """Set ``api.encryption.key``, inserting missing structure; unsafe shapes raise."""
    return _upsert(yaml_text, new_key, record=False, replace=False)[0]


def rotate_api_encryption_key(
    yaml_text: str, new_key: str, *, record_previous: bool, replace_old_key: bool
) -> tuple[str, OldKeyOutcome]:
    """
    ``upsert_api_encryption_key`` that also settles the replaced key as the OTA ``old_key``.

    *record_previous* writes it; an ``old_key`` already there is overwritten only with
    *replace_old_key*. The outcome says what became of the replaced key.
    """
    return _upsert(yaml_text, new_key, record=record_previous, replace=replace_old_key)


def _insert_api_encryption_key(yaml_text: str, new_key: str) -> str:
    """Insert ``api.encryption.key`` where the ``api:`` block has no ``key:`` yet."""
    rendered = _quote(new_key)
    yaml_text, nl = normalize_trailing_newline(yaml_text)
    lines = yaml_text.splitlines(keepends=True)
    located = _locate_top_block(lines, "api")

    if located is None:
        block = (
            f"api:{nl}{ESPHOME_YAML_INDENT}encryption:{nl}"
            f"{ESPHOME_YAML_INDENT * 2}key: {rendered}{nl}"
        )
        return _insert_top_block_after(lines, _API_BLOCK_ANCHORS, block, nl)

    block_start, block_end, indent = located
    enc_idx = _find_encryption_header(lines, block_start, block_end, indent)
    if enc_idx is not None:
        new_line = f"{indent}{ESPHOME_YAML_INDENT}key: {rendered}{nl}"
        return "".join([*lines[: enc_idx + 1], new_line, *lines[enc_idx + 1 :]])

    insert_at = trim_trailing_blanks(lines, block_start, block_end)
    new_lines = [
        f"{indent}encryption:{nl}",
        f"{indent}{ESPHOME_YAML_INDENT}key: {rendered}{nl}",
    ]
    return "".join([*lines[:insert_at], *new_lines, *lines[insert_at:]])


def rewrite_own_ota_encryption_key(yaml_text: str, new_key: str) -> str:
    """Rekey the OTA platform's own literal key when the config has no api key line."""
    if read_yaml_scalar(yaml_text, API_ENCRYPTION_KEY_PATH) is not None:
        return yaml_text
    return rewrite_ota_encryption_key(yaml_text, _literal_swap(new_key))


def _literal_swap(new_key: str) -> Callable[[str], str | None]:
    """Transform replacing a literal ``key:`` with *new_key*; ``!secret`` / ``${…}`` stay."""
    rendered = _quote(new_key)

    def _swap(raw: str) -> str | None:
        return None if is_indirected_scalar(raw) else rendered

    return _swap


def _upsert(
    yaml_text: str, new_key: str, *, record: bool, replace: bool
) -> tuple[str, OldKeyOutcome]:
    existing = read_yaml_scalar(yaml_text, API_ENCRYPTION_KEY_PATH)
    if existing is not None and _strip_yaml_quotes(existing):
        return _rewrite(
            yaml_text, new_key, plain_literal_value(existing), record=record, replace=replace
        )
    # An empty ``key:`` is a runtime-provisioned shape like a bare ``encryption:``;
    # the device then requires the OTA platform's own key, which must not change.
    if _ota_key_competes(yaml_text, new_key):
        raise YamlUpsertNotSupportedError(
            "the config gives the OTA platform its own encryption key, which the "
            "device requires, so no api key was written."
        )
    inserted = (
        rewrite_yaml_scalar(yaml_text, API_ENCRYPTION_KEY_PATH, _literal_swap(new_key))
        if existing is not None
        else _insert_api_encryption_key(yaml_text, new_key)
    )
    return _follow_ota_key(inserted, new_key, None, record=record, replace=replace)


def _rewrite(
    yaml_text: str, new_key: str, previous_key: str | None, *, record: bool, replace: bool
) -> tuple[str, OldKeyOutcome]:
    rewritten = rewrite_yaml_scalar(yaml_text, API_ENCRYPTION_KEY_PATH, _literal_swap(new_key))
    return _follow_ota_key(rewritten, new_key, previous_key, record=record, replace=replace)


def _follow_ota_key(
    yaml_text: str, new_key: str, previous_key: str | None, *, record: bool, replace: bool
) -> tuple[str, OldKeyOutcome]:
    """Reconcile the OTA block with a literal api key of *new_key*; an indirected OTA key raises."""
    if not _literal_key_matches(read_yaml_scalar(yaml_text, API_ENCRYPTION_KEY_PATH), new_key):
        return yaml_text, OldKeyOutcome.NOT_NEEDED
    ota_key = read_ota_encryption_key(yaml_text)
    if ota_key is not None and is_indirected_scalar(ota_key):
        raise YamlUpsertNotSupportedError(
            "the OTA encryption key is provided via !secret, !include, or a substitution "
            "and must match the api encryption key."
        )
    # Before the key drop so the new line takes its indent from the existing one.
    yaml_text, outcome = _settle_old_key(
        yaml_text, new_key, previous_key, record=record, replace=replace
    )
    if ota_key is None:
        return yaml_text, outcome
    # Dropped, not copied: with a static api key the firmware encrypts OTA with
    # that key, and the bare block is the documented shape.
    return drop_ota_encryption_key(yaml_text), outcome


def _settle_old_key(
    yaml_text: str, new_key: str, previous_key: str | None, *, record: bool, replace: bool
) -> tuple[str, OldKeyOutcome]:
    """Record *previous_key* as ``old_key:``; one equal to *new_key* is dropped."""
    old_key = read_ota_encryption_old_key(yaml_text)
    if old_key is not None and _literal_key_matches(old_key, new_key):
        yaml_text, old_key = drop_ota_encryption_old_key(yaml_text), None
    if previous_key is None or previous_key == new_key:
        outcome = OldKeyOutcome.NOT_NEEDED
    elif old_key is not None and is_indirected_scalar(old_key):
        outcome = OldKeyOutcome.KEPT_INDIRECTED
    elif old_key is not None and _literal_key_matches(old_key, previous_key):
        outcome = OldKeyOutcome.RECORDED
    elif old_key is not None and not replace:
        # The device may still run the key already recorded there.
        outcome = OldKeyOutcome.KEPT_EXISTING
    elif not _has_encryption_block(yaml_text):
        outcome = OldKeyOutcome.NO_ENCRYPTION_BLOCK
    elif not record:
        outcome = OldKeyOutcome.NOT_RECORDED
    else:
        return set_ota_encryption_old_key(yaml_text, previous_key), OldKeyOutcome.RECORDED
    return yaml_text, outcome


def _literal_key_matches(raw: str | None, key: str) -> bool:
    """Whether the raw YAML scalar *raw* is the literal *key*."""
    return raw is not None and _strip_yaml_quotes(raw) == key


def _ota_key_competes(yaml_text: str, key: str) -> bool:
    """Whether a non-empty literal OTA key differs from *key*; empty and indirected ones don't."""
    return plain_literal_value(read_ota_encryption_key(yaml_text)) not in (None, key)


def _ota_key_matches(yaml_text: str, key: str) -> bool:
    """Whether an explicit OTA key, if any, is *key* and no literal ``old_key`` equals it."""
    ota_key, old_key = read_ota_encryption_keys(yaml_text)
    if old_key is not None and _literal_key_matches(old_key, key):
        return False
    return ota_key is None or _literal_key_matches(ota_key, key) or is_indirected_scalar(ota_key)


def _find_encryption_header(
    lines: list[str], block_start: int, block_end: int, indent: str
) -> int | None:
    """Find the ``encryption:`` header at *indent* inside the ``api:`` block span."""
    header_re, scalar_re = key_line_res("encryption", prefix=f"^{re.escape(indent)}")
    for i in range(block_start + 1, block_end):
        content = lines[i].rstrip("\n\r")
        if header_re.match(content):
            return i
        if scalar_re.match(content):
            raise YamlUpsertNotSupportedError(
                "api.encryption uses an inline value or flow-style mapping; "
                "the line-based upsert can't safely edit it."
            )
    return None
