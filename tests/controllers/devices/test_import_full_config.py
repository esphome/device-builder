"""Tests for the ``?full_config`` fetch and the MAC-suffix pinning rewrite."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Self
from unittest.mock import Mock

import aiohttp
import pytest

from esphome_device_builder.controllers.devices import import_full_config
from esphome_device_builder.controllers.devices.import_full_config import (
    fetch_full_config,
    materialize_full_config,
)
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.yaml import YamlUpsertNotSupportedError
from esphome_device_builder.models import ErrorCode


def _http_error(status: int) -> aiohttp.ClientResponseError:
    return aiohttp.ClientResponseError(Mock(), (), status=status, message="HTTP")


_LITERAL = (
    "# vendor header\n"
    "esphome:\n"
    "  name: neato  # base name\n"
    "  friendly_name: Neato Speaker\n"
    "  name_add_mac_suffix: true\n"
    "\n"
    "logger:\n"
)

_SUBSTITUTED = (
    "substitutions:\n"
    "  devicename: neato\n"
    '  devicefriendly: "Neato Speaker"\n'
    "\n"
    "esphome:\n"
    "  name: ${devicename}\n"
    "  friendly_name: ${devicefriendly}\n"
    "  name_add_mac_suffix: yes\n"
)


def test_literal_name_is_pinned_and_suffix_disabled() -> None:
    out = materialize_full_config(_LITERAL, "neato-33abec", None)

    assert out == _LITERAL.replace("name: neato  #", "name: neato-33abec  #").replace(
        "name_add_mac_suffix: true", "name_add_mac_suffix: false"
    )


def test_literal_friendly_name_is_applied_when_given() -> None:
    out = materialize_full_config(_LITERAL, "neato-33abec", "Speaker 33abec")

    assert "friendly_name: Speaker 33abec\n" in out


def test_substituted_name_rewrites_the_substitution_not_the_leaf() -> None:
    out = materialize_full_config(_SUBSTITUTED, "neato-33abec", "Speaker 33abec")

    assert "  devicename: neato-33abec\n" in out
    assert "  devicefriendly: Speaker 33abec\n" in out
    assert "  name: ${devicename}\n" in out
    assert "  friendly_name: ${devicefriendly}\n" in out
    assert "  name_add_mac_suffix: false\n" in out


def test_absent_friendly_name_leaf_is_inserted() -> None:
    upstream = "esphome:\n  name: neato\n  name_add_mac_suffix: true\n"

    out = materialize_full_config(upstream, "neato-33abec", "Speaker 33abec")

    assert out == (
        "esphome:\n  name: neato-33abec\n  name_add_mac_suffix: false\n"
        "  friendly_name: Speaker 33abec\n"
    )


def test_friendly_name_upsert_refusal_is_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        import_full_config,
        "upsert_yaml_leaf_under_top_block",
        Mock(side_effect=YamlUpsertNotSupportedError("flow-style esphome block")),
    )

    with pytest.raises(CommandError) as excinfo:
        materialize_full_config(_LITERAL, "neato-33abec", "Speaker 33abec")

    assert excinfo.value.code == ErrorCode.INVALID_ARGS


@pytest.mark.parametrize(
    "upstream",
    [
        pytest.param("esphome:\n  name: neato\n", id="no_suffix_key"),
        pytest.param("esphome:\n  name: neato\n  name_add_mac_suffix: false\n", id="suffix_off"),
        pytest.param(
            "substitutions:\n  name: audio-1\npackages:\n  board: !include boards/rev2_4.yaml\n",
            id="esphome_block_in_package",
        ),
    ],
)
def test_other_shapes_are_returned_verbatim(upstream: str) -> None:
    assert materialize_full_config(upstream, "neato-33abec", "Speaker") == upstream


@pytest.mark.parametrize(
    "upstream",
    [
        pytest.param("esphome:\n  name_add_mac_suffix: true\n", id="no_name"),
        pytest.param(
            "packages:\n  subs: !include subs.yaml\nesphome:\n  name: ${devicename}\n"
            "  name_add_mac_suffix: true\n",
            id="nonlocal_substitution",
        ),
        pytest.param(
            "esphome:\n  name: ${prefix}-audio\n  name_add_mac_suffix: true\n",
            id="embedded_substitution",
        ),
    ],
)
def test_unpinnable_name_is_refused(upstream: str) -> None:
    with pytest.raises(CommandError) as excinfo:
        materialize_full_config(upstream, "neato-33abec", None)

    assert excinfo.value.code == ErrorCode.INVALID_ARGS


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("not a shorthand", id="malformed"),
        pytest.param("github://x/y/z.yaml?full_config", id="no_ref"),
        pytest.param("bitbucket://x/y/z.yaml@main?full_config", id="unknown_domain"),
    ],
)
async def test_fetch_refuses_unusable_urls(url: str) -> None:
    with pytest.raises(CommandError) as excinfo:
        await fetch_full_config(url)

    assert excinfo.value.code == ErrorCode.INVALID_ARGS


class _FakeSession:
    """``aiohttp.ClientSession`` stand-in whose ``get`` yields *body* or raises *exc*."""

    body: bytes = b""
    exc: Exception | None = None

    def __init__(self, **_kw: Any) -> None:
        pass

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def get(self, _url: str, **_kw: Any) -> Self:
        if self.exc is not None:
            raise self.exc
        return self

    @property
    def content(self) -> Self:
        return self

    async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
        for start in range(0, len(self.body), size):
            yield self.body[start : start + size]


def _serve(
    monkeypatch: pytest.MonkeyPatch, *, body: bytes = b"", exc: Exception | None = None
) -> None:
    monkeypatch.setattr(_FakeSession, "body", body)
    monkeypatch.setattr(_FakeSession, "exc", exc)
    monkeypatch.setattr(import_full_config.aiohttp, "ClientSession", _FakeSession)


async def test_fetch_returns_the_raw_file(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, body="esphome:\n  name: neato\n  comment: Küche\n".encode())

    assert await fetch_full_config("github://x/y/z.yaml@main?full_config") == (
        "esphome:\n  name: neato\n  comment: Küche\n"
    )


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        pytest.param(aiohttp.ClientConnectionError("refused"), ErrorCode.UNAVAILABLE, id="connect"),
        pytest.param(TimeoutError(), ErrorCode.UNAVAILABLE, id="timeout"),
        pytest.param(_http_error(404), ErrorCode.INVALID_ARGS, id="http_404"),
        pytest.param(_http_error(429), ErrorCode.RATE_LIMITED, id="http_429"),
        pytest.param(_http_error(503), ErrorCode.UNAVAILABLE, id="http_503"),
    ],
)
async def test_fetch_failures_are_typed(
    monkeypatch: pytest.MonkeyPatch, exc: Exception, code: ErrorCode
) -> None:
    _serve(monkeypatch, exc=exc)

    with pytest.raises(CommandError) as excinfo:
        await fetch_full_config("github://x/y/z.yaml@main?full_config")

    assert excinfo.value.code == code


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"esphome:\n  name: x\n" + b"#" * (1 << 20), id="oversize"),
        pytest.param(b"esphome:\n  name: \xff\n", id="not_utf8"),
        pytest.param(b"<html><body>captive portal</body></html>\n: [", id="not_yaml"),
        pytest.param(b"- just\n- a list\n", id="not_a_mapping"),
    ],
)
async def test_unusable_bodies_are_refused(monkeypatch: pytest.MonkeyPatch, body: bytes) -> None:
    _serve(monkeypatch, body=body)

    with pytest.raises(CommandError) as excinfo:
        await fetch_full_config("github://x/y/z.yaml@main?full_config")

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
