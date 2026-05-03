"""Regression test for the ``friendly_name_slugify`` import path.

esphome/esphome#16206 moves ``friendly_name_slugify`` from
``esphome.dashboard.util.text`` to ``esphome.helpers`` so it
survives the legacy dashboard's eventual removal. We import it via
a try-new-then-fallback shim — this test pins down that the symbol
imported from the controller resolves to the *same* function
regardless of which esphome release the user has installed, so a
silent drift between the two locations can't sneak past CI.
"""

from __future__ import annotations

from esphome_device_builder.controllers.devices.helpers import friendly_name_slugify


def test_friendly_name_slugify_resolves_via_helpers_or_dashboard_shim() -> None:
    """The slugifier the controller uses is one of the two known sources.

    Asserting object identity rules out a third party (or a future
    accidental rename / re-implementation) sneaking in. Lock the
    contract here so an esphome refactor that drops the symbol from
    both locations fails loudly on import — not at the first
    adoption flow that calls it.
    """
    # Each import is gated by try/except ImportError to tolerate
    # whichever esphome release ships the symbol — keeping them inside
    # the test (PLC0415 noqa) is the point: a top-level failing import
    # would prevent collection, masking the real test failure.
    sources: list = []
    try:
        from esphome.helpers import friendly_name_slugify as helpers_impl  # noqa: PLC0415

        sources.append(helpers_impl)
    except ImportError:
        pass
    try:
        from esphome.dashboard.util.text import (  # noqa: PLC0415
            friendly_name_slugify as dashboard_impl,
        )

        sources.append(dashboard_impl)
    except ImportError:
        pass

    assert sources, "friendly_name_slugify missing from both helpers and dashboard.util.text"
    assert friendly_name_slugify in sources


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
