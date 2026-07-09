"""Defensive-branch coverage for ``DeviceScanner``.

The ordering / index tests in ``test_device_scanner_order.py``
exercise the happy path and a couple of failure modes
(``load_device_from_storage`` raising). The branches pinned here
are smaller — early returns, OSError handlers, and the ``by_path``
read-only accessor — that don't fit the ordering narrative but
keep regressions in the scanner's failure-mode behaviour from
slipping through. A failing scan would silently degrade dashboard
state (devices missing, mDNS dedupe wrong, restart-time crash on
an unreadable YAML); these tests pin the silent-skip behaviour
so the regression surfaces in CI rather than in a user's logs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from esphome_device_builder.controllers._device_scanner import (
    DeviceFileMetadata,
    DeviceScanner,
    ScanChange,
)
from esphome_device_builder.models import Device


def _stub_metadata(_config_dir: Path, _filename: str) -> DeviceFileMetadata:
    return DeviceFileMetadata(board_id="", ip="", expected_config_hash="")


def _make_scanner(
    config_dir: Path,
) -> tuple[DeviceScanner, list[tuple[ScanChange, Device, Device | None]]]:
    """Build a scanner with a recording on_change callback."""
    events: list[tuple[ScanChange, Device, Device | None]] = []
    scanner = DeviceScanner(
        config_dir=config_dir,
        get_metadata=_stub_metadata,
        on_change=lambda kind, device, previous: events.append((kind, device, previous)),
    )
    return scanner, events


def _write_yaml(config_dir: Path, name: str) -> Path:
    path = config_dir / f"{name}.yaml"
    path.write_text(f"esphome:\n  name: {name}\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# by_path accessor
# ---------------------------------------------------------------------------


async def test_by_path_returns_live_mapping(tmp_path: Path) -> None:
    """``by_path`` exposes the path → Device dict the scanner maintains.

    Documented as "treat as read-only"; pin that the contents
    reflect the most recent scan and that the keys are the
    absolute YAML paths the scanner walked. Used by callers that
    need to look up a device by configuration filename without
    paying the O(n) cost of iterating ``devices``.
    """
    cfg = tmp_path / "configs"
    cfg.mkdir()
    yaml_path = _write_yaml(cfg, "kitchen")
    with patch(
        "esphome_device_builder.controllers._device_scanner.load_device_from_storage",
        side_effect=lambda path, *_a, **_kw: Device(
            name=path.stem, friendly_name=path.stem, configuration=path.name
        ),
    ):
        scanner, _ = _make_scanner(cfg)
        await scanner.scan()

    by_path = scanner.by_path
    assert list(by_path.keys()) == [yaml_path]
    assert by_path[yaml_path].name == "kitchen"


# ---------------------------------------------------------------------------
# reload — failure-mode branches
# ---------------------------------------------------------------------------


async def test_reload_returns_false_when_loader_fails(tmp_path: Path) -> None:
    """``reload`` returns ``False`` when the loader logs+skips the file.

    ``_load_devices`` swallows exceptions from
    ``load_device_from_storage`` and returns an empty dict for
    that path. The reload-specific guard treats that as
    "couldn't refresh" — pin the False return so callers (e.g. the
    firmware controller's post-build refresh) don't proceed as if
    the device's metadata is fresh after a parse failure.
    """
    cfg = tmp_path / "configs"
    cfg.mkdir()
    _write_yaml(cfg, "kitchen")

    # Initial scan succeeds so the path is tracked.
    with patch(
        "esphome_device_builder.controllers._device_scanner.load_device_from_storage",
        side_effect=lambda path, *_a, **_kw: Device(
            name=path.stem, friendly_name=path.stem, configuration=path.name
        ),
    ):
        scanner, events = _make_scanner(cfg)
        await scanner.scan()
    events.clear()

    # Reload pass: loader raises, ``_load_devices`` swallows + returns {}.
    with patch(
        "esphome_device_builder.controllers._device_scanner.load_device_from_storage",
        side_effect=ValueError("simulated parse failure"),
    ):
        ok = await scanner.reload("kitchen.yaml")

    assert ok is False
    # The scanner did not fire an UPDATED event for the failed reload.
    assert events == []


async def test_reload_swallows_oserror_on_post_load_stat(tmp_path: Path) -> None:
    """A stat failure after a successful load doesn't break the reload.

    ``reload`` re-stats the file after the load to refresh the
    cache key. If the YAML disappears in that race window
    (atomic-save editor, parallel deletion), the stat raises
    OSError — the reload still returns ``True`` because the
    Device was loaded; the cache key just stays stale until the
    next full scan re-evaluates it.
    """
    cfg = tmp_path / "configs"
    cfg.mkdir()
    _write_yaml(cfg, "kitchen")

    with patch(
        "esphome_device_builder.controllers._device_scanner.load_device_from_storage",
        side_effect=lambda path, *_a, **_kw: Device(
            name=path.stem, friendly_name=path.stem, configuration=path.name
        ),
    ):
        scanner, events = _make_scanner(cfg)
        await scanner.scan()
    events.clear()

    # The patched loader bypasses any internal ``stat`` calls inside
    # ``load_device_from_storage``, so the only ``Path.stat`` call
    # the reload makes is the post-load cache-key refresh on the
    # tracked YAML. Fail it unconditionally for that path.
    yaml_path = cfg / "kitchen.yaml"
    real_stat = Path.stat

    def _stat(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == yaml_path:
            raise OSError("simulated stat failure")
        return real_stat(self, *args, **kwargs)

    with (
        patch.object(Path, "stat", _stat),
        patch(
            "esphome_device_builder.controllers._device_scanner.load_device_from_storage",
            side_effect=lambda path, *_a, **_kw: Device(
                name=path.stem, friendly_name="Kitchen Renamed", configuration=path.name
            ),
        ),
    ):
        ok = await scanner.reload("kitchen.yaml")

    assert ok is True
    # RELOADED still fired with the freshly-loaded Device.
    assert [(kind, dev.friendly_name) for kind, dev, _prev in events] == [
        (ScanChange.RELOADED, "Kitchen Renamed")
    ]


# ---------------------------------------------------------------------------
# _build_cache_keys — stat OSError fallback
# ---------------------------------------------------------------------------


async def test_scan_skips_yamls_that_fail_to_stat(tmp_path: Path) -> None:
    """A YAML that ``util.list_yaml_files`` finds but ``stat`` rejects is skipped.

    Real-world trigger: a broken symlink in the config dir, or a
    file the dashboard's user can list (read on the directory)
    but not stat (no read on the file itself, e.g. mode 0000 or
    a parent path that lost the +x bit between the listdir and
    the stat). Without the OSError handler the scan would crash
    and every other device on disk would vanish from the
    dashboard until the operator removed the offending file.
    """
    cfg = tmp_path / "configs"
    cfg.mkdir()
    good = _write_yaml(cfg, "good")
    broken = _write_yaml(cfg, "broken")

    real_stat = Path.stat

    def _stat(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == broken:
            raise OSError("permission denied")
        return real_stat(self, *args, **kwargs)

    with (
        patch.object(Path, "stat", _stat),
        patch(
            "esphome_device_builder.controllers._device_scanner.load_device_from_storage",
            side_effect=lambda path, *_a, **_kw: Device(
                name=path.stem, friendly_name=path.stem, configuration=path.name
            ),
        ),
    ):
        scanner, _ = _make_scanner(cfg)
        await scanner.scan()

    # Only the good YAML lands in the cache.
    assert [d.name for d in scanner.devices] == ["good"]
    assert good in scanner.by_path
    assert broken not in scanner.by_path


# ---------------------------------------------------------------------------
# on_change previous-device argument
# ---------------------------------------------------------------------------


async def test_scan_updated_passes_previous_device(tmp_path: Path) -> None:
    """UPDATED carries the previously indexed Device; ADDED carries None."""
    cfg = tmp_path / "configs"
    cfg.mkdir()
    yaml_path = _write_yaml(cfg, "kitchen")

    with patch(
        "esphome_device_builder.controllers._device_scanner.load_device_from_storage",
        side_effect=lambda path, *_a, **_kw: Device(
            name=path.stem, friendly_name=path.stem, configuration=path.name
        ),
    ):
        scanner, events = _make_scanner(cfg)
        await scanner.scan()
        assert [(kind, prev) for kind, _dev, prev in events] == [(ScanChange.ADDED, None)]
        first_device = events[0][1]
        events.clear()

        # Bump the cache key so the next scan classifies UPDATED.
        yaml_path.write_text("esphome:\n  name: kitchen\n# edit\n", encoding="utf-8")
        await scanner.scan()

    assert [kind for kind, _dev, _prev in events] == [ScanChange.UPDATED]
    # Identity, not equality — an equal rebuilt Device must not pass.
    assert events[0][2] is first_device


async def test_reload_passes_previous_device(tmp_path: Path) -> None:
    """RELOADED carries the pre-reload Device as previous."""
    cfg = tmp_path / "configs"
    cfg.mkdir()
    _write_yaml(cfg, "kitchen")

    with patch(
        "esphome_device_builder.controllers._device_scanner.load_device_from_storage",
        side_effect=lambda path, *_a, **_kw: Device(
            name=path.stem, friendly_name=path.stem, configuration=path.name
        ),
    ):
        scanner, events = _make_scanner(cfg)
        await scanner.scan()
        first_device = events[0][1]
        events.clear()
        assert await scanner.reload("kitchen.yaml") is True

    assert [kind for kind, _dev, _prev in events] == [ScanChange.RELOADED]
    # Identity, not equality — an equal rebuilt Device must not pass.
    assert events[0][2] is first_device


async def test_scan_removed_passes_none_previous(tmp_path: Path) -> None:
    """REMOVED carries None: the fired Device already is the prior indexed one."""
    cfg = tmp_path / "configs"
    cfg.mkdir()
    yaml_path = _write_yaml(cfg, "kitchen")

    with patch(
        "esphome_device_builder.controllers._device_scanner.load_device_from_storage",
        side_effect=lambda path, *_a, **_kw: Device(
            name=path.stem, friendly_name=path.stem, configuration=path.name
        ),
    ):
        scanner, events = _make_scanner(cfg)
        await scanner.scan()
        events.clear()
        yaml_path.unlink()
        await scanner.scan()

    assert [(kind, dev.name, prev) for kind, dev, prev in events] == [
        (ScanChange.REMOVED, "kitchen", None)
    ]
