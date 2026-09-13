"""The ``?full_config`` adoption: fetch the upstream YAML and pin it to one device."""

from __future__ import annotations

import asyncio
import logging

import aiohttp
import yaml

from ...helpers.api import CommandError
from ...helpers.lazy_module import async_import_module
from ...helpers.yaml import (
    ESPHOME_NAME_ADD_MAC_SUFFIX_PATH,
    FastestSafeLoader,
    YamlUpsertNotSupportedError,
    _strip_yaml_quotes,
    parse_config_boolean,
    read_yaml_scalar,
    rewrite_rename_content,
    rewrite_yaml_scalar,
    upsert_yaml_leaf_under_top_block,
)
from ...models import ErrorCode

_LOGGER = logging.getLogger(__name__)

_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=30)
_MAX_ATTEMPTS = 3
_MAX_CONFIG_BYTES = 1 << 20
_READ_CHUNK = 64 * 1024
_TRANSIENT_CODES = frozenset({ErrorCode.UNAVAILABLE, ErrorCode.RATE_LIMITED})
_INCLUDE_TAG_PREFIX = "!include"
_LISTED_INCLUDES = 3
_PIN_REMEDY = (
    "The upstream config adds a MAC suffix, so its name must be pinned to this device "
    "before it can be adopted; create the device by hand from the upstream YAML with "
    "esphome.name set to this device's name instead."
)


async def fetch_full_config(package_import_url: str) -> str:
    """Return the raw YAML the ``github://…?full_config`` shorthand points at."""
    git = await async_import_module("esphome.git")
    try:
        url: str = git.GitFile.from_shorthand(package_import_url).raw_url
    except (ValueError, NotImplementedError) as exc:
        raise CommandError(
            ErrorCode.INVALID_ARGS, f"Unsupported import URL {package_import_url}: {exc}"
        ) from exc
    for attempt in range(1, _MAX_ATTEMPTS):
        try:
            return await _fetch_once(url)
        except CommandError as exc:
            if exc.code not in _TRANSIENT_CODES:
                raise
            delay = 2**attempt
            _LOGGER.warning(
                "Import of %s failed: %s. Retrying in %d seconds... (attempt %d/%d)",
                url,
                exc,
                delay,
                attempt + 1,
                _MAX_ATTEMPTS,
            )
            await asyncio.sleep(delay)
    return await _fetch_once(url)


def materialize_full_config(contents: str, name: str, friendly_name: str | None) -> str:
    """Pin *contents* to the adopted device when its ``esphome:`` adds a MAC suffix."""
    suffix = read_yaml_scalar(contents, ESPHOME_NAME_ADD_MAC_SUFFIX_PATH)
    if suffix is None:
        return contents
    enabled = parse_config_boolean(_strip_yaml_quotes(suffix))
    if enabled is None:
        raise CommandError(
            ErrorCode.INVALID_ARGS,
            f"Can't tell whether the upstream config adds a MAC suffix "
            f"(esphome.name_add_mac_suffix is {suffix}). {_PIN_REMEDY}",
        )
    if not enabled:
        return contents
    text = rewrite_yaml_scalar(contents, ESPHOME_NAME_ADD_MAC_SUFFIX_PATH, lambda _raw: "false")
    text = rewrite_rename_content(text, name, remedy=_PIN_REMEDY)
    if friendly_name:
        try:
            text = upsert_yaml_leaf_under_top_block(text, "esphome", "friendly_name", friendly_name)
        except YamlUpsertNotSupportedError as exc:
            raise CommandError(ErrorCode.INVALID_ARGS, str(exc)) from exc
    return text


def local_includes(contents: str) -> list[str]:
    """Return the ``!include``-tagged paths in *contents*, in document order."""
    found: list[str] = []
    pending = [yaml.compose(contents, Loader=FastestSafeLoader)]
    while pending:
        node = pending.pop()
        if node is None:
            continue
        if node.tag.startswith(_INCLUDE_TAG_PREFIX):
            if isinstance(node, yaml.ScalarNode):
                found.append(node.value)
            elif isinstance(node, yaml.MappingNode):
                found.append(_include_file(node) or node.tag)
        if isinstance(node, yaml.SequenceNode):
            pending.extend(reversed(node.value))
        elif isinstance(node, yaml.MappingNode):
            pending.extend(v for _k, v in reversed(node.value))
    return found


def package_fallback_warning(includes: list[str]) -> str:
    """Explain that the single-file copy gave way to the package import."""
    listed = ", ".join(includes[:_LISTED_INCLUDES])
    if len(includes) > _LISTED_INCLUDES:
        listed += f" and {len(includes) - _LISTED_INCLUDES} more"
    return (
        f"This configuration includes local files ({listed}) that a full-config import "
        "doesn't fetch, so it was imported as a package referencing the vendor's "
        "repository instead."
    )


async def _fetch_once(url: str) -> str:
    try:
        async with (
            aiohttp.ClientSession(timeout=_FETCH_TIMEOUT, trust_env=True) as session,
            session.get(url, raise_for_status=True) as resp,
        ):
            body = await _read_capped(resp, url)
    except aiohttp.ClientResponseError as exc:
        raise CommandError(
            _code_for_status(exc.status), f"{url} returned HTTP {exc.status} {exc.message}"
        ) from exc
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise CommandError(ErrorCode.UNAVAILABLE, f"Could not fetch {url}: {exc}") from exc
    return _decode_yaml_text(body, url)


def _code_for_status(status: int) -> ErrorCode:
    if status == 429:
        return ErrorCode.RATE_LIMITED
    return ErrorCode.INVALID_ARGS if status < 500 else ErrorCode.UNAVAILABLE


async def _read_capped(resp: aiohttp.ClientResponse, url: str) -> bytes:
    """Read the body, refusing one over ``_MAX_CONFIG_BYTES`` before buffering it all."""
    body = bytearray()
    async for chunk in resp.content.iter_chunked(_READ_CHUNK):
        body += chunk
        if len(body) > _MAX_CONFIG_BYTES:
            raise CommandError(
                ErrorCode.INVALID_ARGS, f"{url} is larger than {_MAX_CONFIG_BYTES} bytes"
            )
    return bytes(body)


def _decode_yaml_text(body: bytes, url: str) -> str:
    """Return *body* as text once it decodes and composes to a YAML mapping."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CommandError(ErrorCode.INVALID_ARGS, f"{url} is not UTF-8 text") from exc
    try:
        root = yaml.compose(text, Loader=FastestSafeLoader)
    except yaml.YAMLError as exc:
        raise CommandError(ErrorCode.INVALID_ARGS, f"{url} is not valid YAML: {exc}") from exc
    if not isinstance(root, yaml.MappingNode):
        raise CommandError(ErrorCode.INVALID_ARGS, f"{url} is not a YAML mapping")
    return text


def _include_file(node: yaml.MappingNode) -> str | None:
    """Return the scalar ``file`` of an ``!include {file: …, vars: …}`` mapping, or none."""
    for key, value in node.value:
        if (
            isinstance(key, yaml.ScalarNode)
            and key.value == "file"
            and isinstance(value, yaml.ScalarNode)
        ):
            return str(value.value)
    return None
