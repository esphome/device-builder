"""The ``?full_config`` adoption: fetch the upstream YAML and pin it to one device."""

from __future__ import annotations

import aiohttp

from ...helpers.api import CommandError
from ...helpers.device_yaml import yaml_has_name_add_mac_suffix
from ...helpers.lazy_module import async_import_module
from ...helpers.yaml import (
    YamlUpsertNotSupportedError,
    rewrite_rename_content,
    rewrite_yaml_scalar,
    upsert_yaml_leaf_under_top_block,
)
from ...models import ErrorCode

_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=30)
_NAME_ADD_MAC_SUFFIX_PATH = ("esphome", "name_add_mac_suffix")
_PIN_REMEDY = (
    "The upstream config adds a MAC suffix, so its name must be pinned to this device "
    "before it can be adopted."
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
    try:
        async with (
            aiohttp.ClientSession(timeout=_FETCH_TIMEOUT) as session,
            session.get(url, raise_for_status=True) as resp,
        ):
            return await resp.text()
    except aiohttp.ClientResponseError as exc:
        raise CommandError(
            ErrorCode.INVALID_ARGS, f"{url} returned HTTP {exc.status} {exc.message}"
        ) from exc
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise CommandError(ErrorCode.UNAVAILABLE, f"Could not fetch {url}: {exc}") from exc


def materialize_full_config(contents: str, name: str, friendly_name: str | None) -> str:
    """Pin *contents* to the adopted device when its ``esphome:`` adds a MAC suffix."""
    if not yaml_has_name_add_mac_suffix(contents):
        return contents
    text = rewrite_yaml_scalar(contents, _NAME_ADD_MAC_SUFFIX_PATH, lambda _raw: "false")
    text = rewrite_rename_content(text, name, remedy=_PIN_REMEDY)
    if friendly_name:
        try:
            text = upsert_yaml_leaf_under_top_block(text, "esphome", "friendly_name", friendly_name)
        except YamlUpsertNotSupportedError as exc:
            raise CommandError(ErrorCode.INVALID_ARGS, str(exc)) from exc
    return text
