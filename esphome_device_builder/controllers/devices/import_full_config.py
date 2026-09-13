"""The ``?full_config`` adoption: fetch the upstream YAML and pin it to one device."""

from __future__ import annotations

import aiohttp

from ...helpers.api import CommandError
from ...helpers.lazy_module import async_import_module
from ...helpers.yaml import (
    ESPHOME_NAME_PATH,
    parse_config_boolean,
    read_yaml_scalar,
    rewrite_name_or_substitution,
    rewrite_yaml_scalar,
)
from ...models import ErrorCode

_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=30)
_NAME_ADD_MAC_SUFFIX_PATH = ("esphome", "name_add_mac_suffix")
_FRIENDLY_NAME_PATH = ("esphome", "friendly_name")


async def fetch_full_config(package_import_url: str) -> str:
    """Return the raw YAML the ``github://…?full_config`` shorthand points at."""
    git = await async_import_module("esphome.git")
    try:
        url: str = git.GitFile.from_shorthand(package_import_url).raw_url
    except (ValueError, NotImplementedError) as exc:
        raise CommandError(
            ErrorCode.INVALID_ARGS, f"Unsupported import URL {package_import_url}: {exc}"
        ) from exc
    try:
        async with (
            aiohttp.ClientSession(timeout=_FETCH_TIMEOUT) as session,
            session.get(url, raise_for_status=True) as resp,
        ):
            return await resp.text()
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise CommandError(ErrorCode.UNAVAILABLE, f"Could not fetch {url}: {exc}") from exc


def materialize_full_config(contents: str, name: str, friendly_name: str | None) -> str:
    """
    Pin *contents* to the adopted device when its top-level ``esphome:`` adds a MAC suffix.

    Any other shape is returned verbatim; the ``esphome:`` block may live in a package.
    """
    suffix = read_yaml_scalar(contents, _NAME_ADD_MAC_SUFFIX_PATH)
    if suffix is None or parse_config_boolean(suffix) is not True:
        return contents
    if read_yaml_scalar(contents, ESPHOME_NAME_PATH) is None:
        raise CommandError(
            ErrorCode.INVALID_ARGS,
            "The upstream config enables name_add_mac_suffix but declares no esphome.name "
            "to pin to this device.",
        )
    text = rewrite_yaml_scalar(contents, _NAME_ADD_MAC_SUFFIX_PATH, lambda _raw: "false")
    text = rewrite_name_or_substitution(text, ESPHOME_NAME_PATH, name)
    if friendly_name is not None and read_yaml_scalar(text, _FRIENDLY_NAME_PATH) is not None:
        text = rewrite_name_or_substitution(text, _FRIENDLY_NAME_PATH, friendly_name)
    return text
