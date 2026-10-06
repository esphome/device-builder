"""The ``?full_config`` adoption: fetch the upstream YAML and pin it to one device."""

from __future__ import annotations

import asyncio
import logging
from typing import NamedTuple

import aiohttp
import yaml
from esphome.const import CONF_FILE

from ...helpers.api import CommandError
from ...helpers.lazy_module import async_import_module
from ...helpers.text import summarise
from ...helpers.yaml import (
    ESPHOME_NAME_ADD_MAC_SUFFIX_PATH,
    FastestSafeLoader,
    YamlUpsertNotSupportedError,
    parse_config_boolean,
    read_yaml_scalar,
    rewrite_rename_content,
    rewrite_yaml_scalar,
    upsert_yaml_leaf_under_top_block,
)
from ...models import ErrorCode

_LOGGER = logging.getLogger(__name__)

# Module-local seams so tests patch this module, not aiohttp or asyncio.
_new_session = aiohttp.ClientSession
_sleep = asyncio.sleep

_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=30)
_MAX_ATTEMPTS = 3
_MAX_CONFIG_BYTES = 1 << 20
_READ_CHUNK = 64 * 1024
_TRANSIENT_CODES = frozenset({ErrorCode.UNAVAILABLE, ErrorCode.RATE_LIMITED})
_INCLUDE_TAG_PREFIX = "!include"
_PIN_REMEDY = (
    "The upstream config adds a MAC suffix, so its name must be pinned to this device "
    "before it can be adopted; create the device by hand from the upstream YAML with "
    "esphome.name set to this device's name instead."
)


class FetchedConfig(NamedTuple):
    """An upstream YAML document: its text and the node tree it composes to."""

    text: str
    root: yaml.MappingNode


async def fetch_full_config(package_import_url: str) -> FetchedConfig:
    """Return the YAML the ``github://…?full_config`` shorthand points at."""
    git = await async_import_module("esphome.git")
    try:
        url: str = git.GitFile.from_shorthand(package_import_url).raw_url
    except (ValueError, NotImplementedError) as exc:
        raise CommandError(
            ErrorCode.INVALID_ARGS, f"Unsupported import URL {package_import_url}: {exc}"
        ) from exc
    attempt = 0
    while True:
        attempt += 1
        try:
            return await _fetch_once(url)
        except CommandError as exc:
            if exc.code not in _TRANSIENT_CODES or attempt == _MAX_ATTEMPTS:
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
            await _sleep(delay)


def parse_full_config(text: str, source: str = "the upstream config") -> FetchedConfig:
    """Compose *text*, refusing anything but a YAML mapping."""
    try:
        root = yaml.compose(text, Loader=FastestSafeLoader)
    except yaml.YAMLError as exc:
        raise CommandError(ErrorCode.INVALID_ARGS, f"{source} is not valid YAML: {exc}") from exc
    if not isinstance(root, yaml.MappingNode):
        raise CommandError(ErrorCode.INVALID_ARGS, f"{source} is not a YAML mapping")
    return FetchedConfig(text, root)


def materialize_full_config(fetched: FetchedConfig, name: str, friendly_name: str | None) -> str:
    """Pin *fetched* to the adopted device: its MAC-suffixed name and the chosen friendly name."""
    text = fetched.text
    suffix = read_yaml_scalar(text, ESPHOME_NAME_ADD_MAC_SUFFIX_PATH)
    if suffix is None and _node_at(fetched.root, *ESPHOME_NAME_ADD_MAC_SUFFIX_PATH) is not None:
        raise CommandError(
            ErrorCode.INVALID_ARGS,
            "The upstream config spells esphome.name_add_mac_suffix in a form the "
            f"adoption can't rewrite. {_PIN_REMEDY}",
        )
    # An indirected value (``${var}``) pins as well: under either resolution the
    # broadcast name is the one to keep.
    if suffix is not None and parse_config_boolean(suffix) is not False:
        text = rewrite_yaml_scalar(text, ESPHOME_NAME_ADD_MAC_SUFFIX_PATH, lambda _raw: "false")
        text = rewrite_rename_content(text, name, remedy=_PIN_REMEDY)
    if friendly_name:
        try:
            text = upsert_yaml_leaf_under_top_block(text, "esphome", "friendly_name", friendly_name)
        except YamlUpsertNotSupportedError as exc:
            raise CommandError(ErrorCode.INVALID_ARGS, str(exc)) from exc
    return text


def local_includes(fetched: FetchedConfig) -> list[str]:
    """Return the ``!include``-tagged paths in *fetched*, in document order."""
    found: list[str] = []
    seen: set[int] = set()
    pending: list[yaml.Node] = [fetched.root]
    while pending:
        node = pending.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if node.tag.startswith(_INCLUDE_TAG_PREFIX):
            if isinstance(node, yaml.ScalarNode):
                found.append(node.value)
            elif isinstance(node, yaml.MappingNode) and (file := _include_file(node)):
                found.append(file)
        if isinstance(node, yaml.SequenceNode):
            pending.extend(reversed(node.value))
        elif isinstance(node, yaml.MappingNode):
            pending.extend(v for _k, v in reversed(node.value))
    return found


def package_fallback_warning(includes: list[str]) -> str:
    """Explain that the single-file copy gave way to the package import."""
    return (
        f"This configuration includes local files ({summarise(includes)}) that a full-config "
        "import doesn't fetch, so it was imported as a package referencing the vendor's "
        "repository instead."
    )


async def _fetch_once(url: str) -> FetchedConfig:
    try:
        async with (
            _new_session(timeout=_FETCH_TIMEOUT, trust_env=True) as session,
            session.get(url, raise_for_status=True) as resp,
        ):
            body = await _read_capped(resp, url)
    except aiohttp.ClientResponseError as exc:
        raise CommandError(
            _code_for_status(exc.status), f"{url} returned HTTP {exc.status} {exc.message}"
        ) from exc
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise CommandError(ErrorCode.UNAVAILABLE, f"Could not fetch {url}: {exc}") from exc
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CommandError(ErrorCode.INVALID_ARGS, f"{url} is not UTF-8 text") from exc
    return parse_full_config(text, url)


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


def _node_at(root: yaml.Node, *keys: str) -> yaml.Node | None:
    """Return the node under the scalar-keyed mapping path *keys*, or none."""
    node = root
    for key in keys:
        if not isinstance(node, yaml.MappingNode):
            return None
        child = next(
            (v for k, v in node.value if isinstance(k, yaml.ScalarNode) and k.value == key),
            None,
        )
        if child is None:
            return None
        node = child
    return node


def _include_file(node: yaml.MappingNode) -> str | None:
    """Return the scalar ``file`` of an ``!include {file: …, vars: …}`` mapping, or none."""
    file = _node_at(node, CONF_FILE)
    return str(file.value) if isinstance(file, yaml.ScalarNode) else None
