"""web_server's ``auth`` group renders on the main form, not under advanced."""

from __future__ import annotations

import json
from pathlib import Path

from script.sync_components import (  # type: ignore[import-not-found]
    _classify_advanced,
)

_BODIES_DIR = (
    Path(__file__).resolve().parent.parent / "esphome_device_builder" / "definitions" / "components"
)


def _web_server_entries() -> dict[str, dict]:
    body = json.loads((_BODIES_DIR / "web_server.json").read_text(encoding="utf-8"))
    return {entry["key"]: entry for entry in body["config_entries"]}


def test_auth_classifies_main_form() -> None:
    assert _classify_advanced("auth", required=False, is_structural=False) is False


def test_other_optional_keys_stay_advanced() -> None:
    assert _classify_advanced("local", required=False, is_structural=False) is True


def test_committed_body_pins_auth_on_main_form() -> None:
    auth = _web_server_entries()["auth"]
    assert not auth.get("advanced")
    children = {c["key"]: c for c in auth["config_entries"]}
    assert {"username", "password"} <= set(children)
    for key in ("username", "password"):
        assert children[key]["required"] is True
        assert not children[key].get("advanced")


def test_committed_body_auth_type_options_name_the_values() -> None:
    auth = _web_server_entries()["auth"]
    children = {c["key"]: c for c in auth["config_entries"]}
    type_entry = children["type"]
    assert type_entry["default_value"] == "basic"
    options = {o["label"]: o for o in type_entry["options"]}
    assert set(options) == {"basic", "digest"}
    for option in options.values():
        assert option["label"] == option["value"]
        assert option["description"]


def test_committed_body_keeps_siblings_advanced() -> None:
    entries = _web_server_entries()
    assert entries["port"]["advanced"] is True
    assert entries["local"]["advanced"] is True
