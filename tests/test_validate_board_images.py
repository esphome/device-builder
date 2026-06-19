"""Tests for the opt-in image-reachability check in ``script/validate_definitions.py``.

Drive ``check_board_images`` with synthetic manifests on a tmp dir and an
injected fetcher so classification is exercised without any real network.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from script.validate_definitions import (  # type: ignore[import-not-found]
    _collect_board_image_urls,
    check_board_images,
)


def _write_board(boards_dir: Path, board_id: str, images: list[str] | None) -> None:
    manifest_dir = boards_dir / board_id
    manifest_dir.mkdir(parents=True)
    data: dict = {"id": board_id, "name": board_id}
    if images is not None:
        data["images"] = images
    (manifest_dir / "manifest.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def test_reachable_image_passes(tmp_path: Path) -> None:
    _write_board(tmp_path, "alpha", ["https://example.test/a.jpg"])
    errors = check_board_images(tmp_path, fetch=lambda _url: 200)
    assert errors == []


def test_404_reports_error(tmp_path: Path) -> None:
    _write_board(tmp_path, "alpha", ["https://example.test/dead.jpg"])
    errors = check_board_images(tmp_path, fetch=lambda _url: 404)
    assert errors == ["alpha: image https://example.test/dead.jpg -> 404"]


def test_network_exception_reports_error(tmp_path: Path) -> None:
    def _boom(_url: str) -> int:
        raise OSError("name resolution failed")

    _write_board(tmp_path, "alpha", ["https://example.test/x.jpg"])
    errors = check_board_images(tmp_path, fetch=_boom)
    assert errors == ["alpha: image https://example.test/x.jpg -> error: name resolution failed"]


def test_shared_url_reports_each_board(tmp_path: Path) -> None:
    shared = "https://example.test/shared.jpg"
    _write_board(tmp_path, "alpha", [shared])
    _write_board(tmp_path, "beta", [shared])
    errors = check_board_images(tmp_path, fetch=lambda _url: 404)
    assert sorted(errors) == [
        f"alpha: image {shared} -> 404",
        f"beta: image {shared} -> 404",
    ]


def test_dedup_fetches_shared_url_once(tmp_path: Path) -> None:
    shared = "https://example.test/shared.jpg"
    _write_board(tmp_path, "alpha", [shared])
    _write_board(tmp_path, "beta", [shared])
    calls: list[str] = []

    def _record(url: str) -> int:
        calls.append(url)
        return 200

    check_board_images(tmp_path, fetch=_record)
    assert calls == [shared]


def test_non_http_and_missing_images_skipped(tmp_path: Path) -> None:
    _write_board(tmp_path, "alpha", ["images/local.jpg"])
    _write_board(tmp_path, "beta", None)
    assert _collect_board_image_urls(tmp_path) == {}
    errors = check_board_images(tmp_path, fetch=lambda _url: 500)
    assert errors == []
