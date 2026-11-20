"""Local schema bundles use the installed ESPHome version."""

from __future__ import annotations

from pathlib import Path

import esphome.const
import pytest

from script import sync_components


def test_local_schema_reaches_catalog_without_release_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit bundle bypasses release discovery and download."""
    monkeypatch.setattr(
        "sys.argv",
        [
            "sync_components.py",
            "--schema-dir",
            str(tmp_path),
            "--version",
            esphome.const.__version__,
        ],
    )

    def no_network(*args: object, **kwargs: object) -> None:
        pytest.fail("Local schemas must not fetch a release bundle")

    class CatalogReachedError(Exception):
        pass

    def build_catalog(*, schema_dir: Path, limit: object) -> None:
        assert schema_dir == tmp_path
        assert limit is None
        raise CatalogReachedError

    monkeypatch.setattr(sync_components, "resolve_latest_release", no_network)
    monkeypatch.setattr(sync_components, "ensure_schema", no_network)
    monkeypatch.setattr(sync_components, "build_catalog", build_catalog)
    with pytest.raises(CatalogReachedError):
        sync_components.main()


def test_local_schema_requires_explicit_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["sync_components.py", "--schema-dir", str(tmp_path)])
    with pytest.raises(SystemExit):
        sync_components.main()
    assert "--schema-dir requires --version" in capsys.readouterr().err


def test_local_schema_keeps_installed_version_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["sync_components.py", "--schema-dir", str(tmp_path), "--version", "2026.6.2"],
    )
    monkeypatch.setattr(esphome.const, "__version__", "2026.6.2b1")
    with pytest.raises(SystemExit, match=r"needs ESPHome 2026\.6\.2"):
        sync_components.main()
