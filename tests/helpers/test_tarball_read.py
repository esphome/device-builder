"""Tests for helpers.tarball_read."""

from __future__ import annotations

import io
import tarfile

import pytest

from esphome_device_builder.helpers.tarball_read import (
    check_member_size,
    parse_json_object,
    read_member,
)


class _CustomError(Exception):
    pass


def _tar_with(members: dict[str, bytes]) -> tarfile.TarFile:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, payload in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    buf.seek(0)
    return tarfile.open(fileobj=buf, mode="r")


def test_check_member_size_rejects_per_member_over_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("esphome_device_builder.helpers.tarball_read.FIRMWARE_MAX_TOTAL_BYTES", 10)
    info = tarfile.TarInfo(name="big.bin")
    info.size = 11
    with pytest.raises(_CustomError, match=r"exceeding FIRMWARE_MAX_TOTAL_BYTES"):
        check_member_size(info, total_so_far=0, error_cls=_CustomError)


def test_check_member_size_rejects_cumulative_over_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("esphome_device_builder.helpers.tarball_read.FIRMWARE_MAX_TOTAL_BYTES", 10)
    info = tarfile.TarInfo(name="second.bin")
    info.size = 6
    with pytest.raises(_CustomError, match=r"cumulative size"):
        check_member_size(info, total_so_far=6, error_cls=_CustomError)


def test_check_member_size_accepts_at_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("esphome_device_builder.helpers.tarball_read.FIRMWARE_MAX_TOTAL_BYTES", 10)
    info = tarfile.TarInfo(name="fits.bin")
    info.size = 4
    check_member_size(info, total_so_far=6, error_cls=_CustomError)


def test_read_member_returns_payload_and_running_total() -> None:
    with _tar_with({"a.bin": b"12345"}) as tar:
        member = tar.getmember("a.bin")
        payload, total = read_member(tar, member, total_so_far=7, error_cls=_CustomError)
    assert payload == b"12345"
    assert total == 12


def test_read_member_rejects_non_regular_member() -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name="some_dir/")
        info.type = tarfile.DIRTYPE
        tar.addfile(info)
    buf.seek(0)
    with tarfile.open(fileobj=buf, mode="r") as tar:
        member = tar.getmembers()[0]
        with pytest.raises(_CustomError, match=r"is not a regular file"):
            read_member(tar, member, total_so_far=0, error_cls=_CustomError)


def test_read_member_enforces_size_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("esphome_device_builder.helpers.tarball_read.FIRMWARE_MAX_TOTAL_BYTES", 3)
    with _tar_with({"a.bin": b"12345"}) as tar:
        member = tar.getmember("a.bin")
        with pytest.raises(_CustomError, match=r"exceeding FIRMWARE_MAX_TOTAL_BYTES"):
            read_member(tar, member, total_so_far=0, error_cls=_CustomError)


def test_parse_json_object_round_trips_a_dict() -> None:
    assert parse_json_object(b'{"a": 1}', label="x.json", error_cls=_CustomError) == {"a": 1}


@pytest.mark.parametrize("payload", [b"null", b"[1]", b'"x"', b"42"], ids=str)
def test_parse_json_object_rejects_non_dict(payload: bytes) -> None:
    with pytest.raises(_CustomError, match=r"x\.json is not a JSON object"):
        parse_json_object(payload, label="x.json", error_cls=_CustomError)


def test_parse_json_object_rejects_invalid_json() -> None:
    with pytest.raises(_CustomError, match=r"x\.json is not valid JSON"):
        parse_json_object(b"{not-json", label="x.json", error_cls=_CustomError)
