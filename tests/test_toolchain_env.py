"""Unit contract for dropping inherited ESP-IDF env overrides."""

from __future__ import annotations

import logging
import os

import pytest

from esphome_device_builder.helpers.toolchain_env import drop_inherited_idf_env


def test_drops_inherited_vars_and_names_each(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Both vars are removed and each dropped var is named with its value in the log."""
    monkeypatch.setenv("IDF_PATH", r"C:\Users\user\.platformio\packages\framework-espidf")
    monkeypatch.setenv("IDF_TOOLS_PATH", r"C:\esp\tools")
    with caplog.at_level(logging.WARNING):
        drop_inherited_idf_env()
    assert "IDF_PATH" not in os.environ
    assert "IDF_TOOLS_PATH" not in os.environ
    assert r"IDF_PATH=C:\Users\user\.platformio\packages\framework-espidf" in caplog.text
    assert r"IDF_TOOLS_PATH=C:\esp\tools" in caplog.text


def test_noop_and_silent_when_absent(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Nothing is logged when neither var is present."""
    monkeypatch.delenv("IDF_PATH", raising=False)
    monkeypatch.delenv("IDF_TOOLS_PATH", raising=False)
    with caplog.at_level(logging.WARNING):
        drop_inherited_idf_env()
    assert caplog.text == ""


def test_drops_only_the_var_that_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """A var that is absent stays absent while the set one is removed."""
    monkeypatch.delenv("IDF_TOOLS_PATH", raising=False)
    monkeypatch.setenv("IDF_PATH", "/opt/esp-idf")
    drop_inherited_idf_env()
    assert "IDF_PATH" not in os.environ
    assert "IDF_TOOLS_PATH" not in os.environ
