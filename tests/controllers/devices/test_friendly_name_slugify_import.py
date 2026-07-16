"""Contract pin for the ``friendly_name_slugify`` the controller uses."""

from __future__ import annotations

from esphome_device_builder.controllers.devices.helpers import friendly_name_slugify


def test_friendly_name_slugify_produces_dashed_lowercase() -> None:
    """Sanity-check the function's contract is what the rest of the code expects.

    The catalog key / on-disk filename routing in
    ``DevicesController`` assumes the result is ``[a-z0-9-]+``
    (no underscores, no spaces, no uppercase). Smoke-test that
    invariant here so a silent upstream refactor that changes
    the slugification rules fails this test instead of corrupting
    filenames at adoption time.
    """
    result = friendly_name_slugify("Living Room Sensor 42")
    assert result == "living-room-sensor-42"
