"""Tests for the ``devices/clone`` command path.

Covers the user-correctable failures (collision, empty / equal name,
missing source) as typed ``CommandError(INVALID_ARGS, …)`` so the
clone dialog can show specific messages rather than a generic
"Command failed" fallback. Also covers the happy path: the new YAML
swaps ``esphome.name`` / ``friendly_name``, regenerates the API
encryption key, leaves ``!secret`` indirections alone, and triggers
a scan so the new file shows up in the next ``devices/list``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode

from .conftest import MakeControllerFactory

SOURCE_YAML = """\
esphome:
  name: kitchen
  friendly_name: Kitchen Lamp

esp32:
  variant: ESP32

logger:

api:
  encryption:
    key: "OLDKEYBASE64BASE64BASE64BASE64BASE64BASE64=="

ota:
  - platform: esphome

wifi:
  ssid: !secret wifi_ssid
  password: !secret wifi_password
"""


@pytest.mark.usefixtures("stub_create_device_metadata_helpers")
async def test_clone_device_writes_new_yaml_and_swaps_name_friendly_key(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Happy path: clone produces a new YAML with fresh identity material.

    Pin all three rewrites in one trace because they're driven by
    the same call: ``esphome.name`` swap, ``friendly_name``
    override (defaulted from ``new_name``), and a fresh
    base64-encoded ``api.encryption.key`` distinct from the
    source's. The scanner gets nudged so the new YAML shows up
    in the next ``devices/list``.
    """
    ctrl = make_controller(tmp_path, with_state_monitor=True, with_boards=True)
    (tmp_path / "kitchen.yaml").write_text(SOURCE_YAML, "utf-8")

    result = await ctrl.clone_device(configuration="kitchen.yaml", new_name="bedroom-bulb")

    assert result == {"configuration": "bedroom-bulb.yaml"}
    new_yaml = (tmp_path / "bedroom-bulb.yaml").read_text("utf-8")
    assert "name: bedroom-bulb\n" in new_yaml
    # Friendly name defaulted from ``friendly_name_slugify(new_name)``.
    assert re.search(r"friendly_name: \S", new_yaml)
    assert "friendly_name: Kitchen Lamp" not in new_yaml
    # Encryption key is fresh — different from the source's literal.
    assert "OLDKEYBASE64BASE64BASE64BASE64BASE64BASE64==" not in new_yaml
    # New key is double-quoted base64 — pinned by ``rewrite_api_encryption_key``.
    assert re.search(r'    key: "[A-Za-z0-9+/=]+"', new_yaml)
    # ``!secret`` indirections preserved.
    assert "ssid: !secret wifi_ssid" in new_yaml
    assert "password: !secret wifi_password" in new_yaml
    # Source file untouched.
    assert (tmp_path / "kitchen.yaml").read_text("utf-8") == SOURCE_YAML
    # Scanner nudged so the new file lands in the next ``devices/list``.
    assert ctrl._scanner.calls == [("scan",)]


@pytest.mark.usefixtures("stub_create_device_metadata_helpers")
async def test_clone_device_uses_explicit_friendly_name_when_provided(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Caller-supplied friendly name lands verbatim in the new YAML."""
    ctrl = make_controller(tmp_path, with_state_monitor=True, with_boards=True)
    (tmp_path / "kitchen.yaml").write_text(SOURCE_YAML, "utf-8")

    await ctrl.clone_device(
        configuration="kitchen.yaml",
        new_name="bedroom-bulb",
        new_friendly_name="Bedroom Reading Lamp",
    )

    new_yaml = (tmp_path / "bedroom-bulb.yaml").read_text("utf-8")
    assert "friendly_name: Bedroom Reading Lamp\n" in new_yaml


@pytest.mark.usefixtures("stub_create_device_metadata_helpers")
async def test_clone_device_skips_friendly_rewrite_when_blank(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """An explicit blank friendly name leaves the source's line untouched.

    Edge case for callers that want the clone to share the source's
    label (rare but harmless to allow). Defaulting is opt-in via
    omission; explicit ``""`` opts out.
    """
    ctrl = make_controller(tmp_path, with_state_monitor=True, with_boards=True)
    (tmp_path / "kitchen.yaml").write_text(SOURCE_YAML, "utf-8")

    await ctrl.clone_device(
        configuration="kitchen.yaml",
        new_name="bedroom-bulb",
        new_friendly_name="",
    )

    new_yaml = (tmp_path / "bedroom-bulb.yaml").read_text("utf-8")
    assert "friendly_name: Kitchen Lamp\n" in new_yaml


async def test_clone_device_rejects_collision_with_existing_filename(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """A clone target that already exists raises ``INVALID_ARGS``."""
    ctrl = make_controller(tmp_path, with_state_monitor=True, with_boards=True)
    (tmp_path / "kitchen.yaml").write_text(SOURCE_YAML, "utf-8")
    (tmp_path / "bedroom-bulb.yaml").write_text("esphome:\n  name: bedroom-bulb\n", "utf-8")

    with pytest.raises(CommandError) as excinfo:
        await ctrl.clone_device(configuration="kitchen.yaml", new_name="bedroom-bulb")

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "bedroom-bulb.yaml already exists" in excinfo.value.message
    # Pre-flight failure: nothing written, scanner not nudged.
    assert ctrl._scanner.calls == []


async def test_clone_device_rejects_empty_new_name(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Whitespace-only ``new_name`` raises ``INVALID_ARGS``."""
    ctrl = make_controller(tmp_path, with_state_monitor=True, with_boards=True)
    (tmp_path / "kitchen.yaml").write_text(SOURCE_YAML, "utf-8")

    with pytest.raises(CommandError) as excinfo:
        await ctrl.clone_device(configuration="kitchen.yaml", new_name="   ")

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "new_name is required" in excinfo.value.message


async def test_clone_device_rejects_same_name_as_source(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Cloning to the same hostname is a no-op + raises ``INVALID_ARGS``."""
    ctrl = make_controller(tmp_path, with_state_monitor=True, with_boards=True)
    (tmp_path / "kitchen.yaml").write_text(SOURCE_YAML, "utf-8")

    with pytest.raises(CommandError) as excinfo:
        await ctrl.clone_device(configuration="kitchen.yaml", new_name="kitchen")

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "must differ" in excinfo.value.message


async def test_clone_device_rejects_missing_source(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """A source filename that doesn't exist raises ``INVALID_ARGS``."""
    ctrl = make_controller(tmp_path, with_state_monitor=True, with_boards=True)

    with pytest.raises(CommandError) as excinfo:
        await ctrl.clone_device(configuration="ghost.yaml", new_name="bedroom-bulb")

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "ghost.yaml not found" in excinfo.value.message


@pytest.mark.usefixtures("stub_create_device_metadata_helpers")
async def test_clone_device_works_when_source_has_no_api_encryption(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """A source without ``api: encryption:`` produces a working plaintext clone.

    The encryption-key rewrite is a no-op when the source doesn't
    use encryption — we deliberately don't *add* a fresh block.
    The user's choice to run plaintext (private network, custom
    auth, no HA) is intentional and forcing encryption onto a
    clone would silently change the security posture.
    """
    ctrl = make_controller(tmp_path, with_state_monitor=True, with_boards=True)
    yaml = "esphome:\n  name: kitchen\n  friendly_name: Kitchen\nesp32:\n  variant: ESP32\n"
    (tmp_path / "kitchen.yaml").write_text(yaml, "utf-8")

    await ctrl.clone_device(configuration="kitchen.yaml", new_name="bedroom-bulb")

    new_yaml = (tmp_path / "bedroom-bulb.yaml").read_text("utf-8")
    assert "name: bedroom-bulb\n" in new_yaml
    # No spurious api/encryption block sneaks in.
    assert "api:" not in new_yaml
    assert "encryption:" not in new_yaml


@pytest.mark.usefixtures("stub_create_device_metadata_helpers")
async def test_clone_device_works_when_api_is_plaintext(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """A source with ``api:`` but no ``encryption:`` block clones plaintext.

    Same reasoning as the no-api-block case — don't upgrade an
    explicitly-plaintext config to encrypted on clone.
    """
    ctrl = make_controller(tmp_path, with_state_monitor=True, with_boards=True)
    yaml = "esphome:\n  name: kitchen\n  friendly_name: Kitchen\napi:\n  password: hunter2\n"
    (tmp_path / "kitchen.yaml").write_text(yaml, "utf-8")

    await ctrl.clone_device(configuration="kitchen.yaml", new_name="bedroom-bulb")

    new_yaml = (tmp_path / "bedroom-bulb.yaml").read_text("utf-8")
    assert "name: bedroom-bulb\n" in new_yaml
    assert "api:\n  password: hunter2\n" in new_yaml
    assert "encryption:" not in new_yaml


@pytest.mark.usefixtures("stub_create_device_metadata_helpers")
async def test_clone_device_redirects_through_substitutions_block(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Wizard / dashboard_import shape: clone updates the substitution.

    When the source uses the standard ESPHome wizard pattern —
    ``esphome.name: ${devicename}`` paired with
    ``substitutions.devicename: kitchen`` — the clone must
    rewrite the substitution rather than the leaf. Rewriting the
    leaf would orphan the substitution and break any other
    consumer of ``${devicename}`` in the same file (sensor names,
    log tags, etc.) by stripping the indirection.
    """
    ctrl = make_controller(tmp_path, with_state_monitor=True, with_boards=True)
    yaml = (
        "substitutions:\n"
        "  devicename: acfloatmonitor32\n"
        "  friendly_name: AC Float Monitor 32\n"
        "esphome:\n"
        "  name: ${devicename}\n"
        "  friendly_name: ${friendly_name}\n"
        "esp32:\n  variant: ESP32\n"
    )
    (tmp_path / "acfloatmonitor32.yaml").write_text(yaml, "utf-8")

    await ctrl.clone_device(
        configuration="acfloatmonitor32.yaml",
        new_name="bedroom-bulb",
        new_friendly_name="Bedroom Bulb",
    )

    new_yaml = (tmp_path / "bedroom-bulb.yaml").read_text("utf-8")
    # Substitutions flipped, leaves still reference the variables —
    # any other consumer of ``${devicename}`` now points at the
    # cloned name automatically.
    assert "  devicename: bedroom-bulb\n" in new_yaml
    assert "  friendly_name: Bedroom Bulb\n" in new_yaml
    assert "  name: ${devicename}\n" in new_yaml
    assert "  friendly_name: ${friendly_name}\n" in new_yaml


@pytest.mark.usefixtures("stub_create_device_metadata_helpers")
async def test_clone_device_lands_new_name_when_yaml_name_diverges_from_filename(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Clone rewrites ``esphome.name`` even when source filename and YAML name disagree.

    Real configs sometimes drift: a file named ``kitchen.yaml`` that
    carries ``esphome.name: my-kitchen-bulb`` (hand-edited, or
    legacy from a previous rename), or ``name: $hostname`` with the
    literal substitution variable in the YAML. Earlier draft
    derived ``old_name`` from the filename and gated the rewrite on
    a value-match — that produced clones whose YAML ``name:`` was
    untouched, leaving the cloned config flashing under the
    *source's* hostname. Pin the unconditional rewrite so the new
    name lands regardless of what the source line said.
    """
    ctrl = make_controller(tmp_path, with_state_monitor=True, with_boards=True)
    yaml = "esphome:\n  name: my-kitchen-bulb\n  friendly_name: Kitchen\nesp32:\n  variant: ESP32\n"
    (tmp_path / "kitchen.yaml").write_text(yaml, "utf-8")

    await ctrl.clone_device(configuration="kitchen.yaml", new_name="bedroom-bulb")

    new_yaml = (tmp_path / "bedroom-bulb.yaml").read_text("utf-8")
    assert "  name: bedroom-bulb\n" in new_yaml
    assert "my-kitchen-bulb" not in new_yaml


@pytest.mark.usefixtures("stub_create_device_metadata_helpers")
async def test_clone_device_preserves_secret_indirection_key(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """``key: !secret api_key`` survives the clone.

    The indirection target is shared with the source on disk
    (``secrets.yaml``), so swapping the indirection name to a
    fresh literal would silently desync the rendered config.
    Pin that the clone leaves the indirection alone — the user
    keeps using whatever ``!secret`` value drives both devices.
    """
    ctrl = make_controller(tmp_path, with_state_monitor=True, with_boards=True)
    yaml = (
        "esphome:\n  name: kitchen\n  friendly_name: Kitchen\n"
        "api:\n  encryption:\n    key: !secret api_key\n"
    )
    (tmp_path / "kitchen.yaml").write_text(yaml, "utf-8")

    await ctrl.clone_device(configuration="kitchen.yaml", new_name="bedroom-bulb")

    new_yaml = (tmp_path / "bedroom-bulb.yaml").read_text("utf-8")
    assert "key: !secret api_key" in new_yaml
