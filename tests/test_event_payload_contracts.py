"""
Wire-shape contract tests for every event-payload :class:`TypedDict`.

Each ``EventType.*`` member's wire shape is declared as a
``TypedDict`` next to the controller that fires it. mypy validates
that fire sites construct payloads matching the declared shape,
but mypy can't see runtime drift inside helpers that build a dict
literal and return it as a TypedDict alias (e.g.
``ReachabilityTracker.snapshot``, where the dict literal is the
source of truth and a missing / extra key would be silently wrong
on the wire).

This file pins the contract at runtime: for every TypedDict, a
sample-payload factory exercises the *actual* code path that
emits the dict (or constructs it via the TypedDict-call syntax)
and asserts the dict's keys equal the TypedDict's declared
``__annotations__``. Adding a field to the model without updating
the emitter — or vice versa — fails this test.

Each TypedDict appears once in :data:`_PAYLOAD_FACTORIES` with a
factory that produces a *real* payload via the production code
path, not a hand-rolled dict. Future event-payload TypedDicts
land their factory here at the same time as the model edit;
:func:`test_event_payload_factories_cover_every_event_data_typeddict`
walks ``models.*`` and asserts every ``*Data(TypedDict)`` is
covered, so a new TypedDict can't silently skip the contract.
"""

from __future__ import annotations

from typing import Any, get_type_hints

import pytest

import esphome_device_builder.models as models_pkg
from esphome_device_builder.controllers._reachability_tracker import (
    MdnsCacheInfo,
    ReachabilityTracker,
)
from esphome_device_builder.models import (
    DeviceEventData,
    DeviceReachabilityData,
    DeviceState,
    DeviceStateChangedData,
    FirmwareJob,
    JobLifecycleData,
    JobOutputData,
    JobProgressData,
    JobStatus,
    JobType,
    RemoteBuildPairingWindowChangedData,
    RemoteBuildPairRequestReceivedData,
    RemoteBuildPairStatusChangedData,
)
from esphome_device_builder.models.devices import Device


def _make_device() -> Device:
    return Device(
        name="kitchen",
        friendly_name="Kitchen",
        configuration="kitchen.yaml",
    )


def _make_job() -> FirmwareJob:
    return FirmwareJob(
        job_id="abc123",
        configuration="kitchen.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.QUEUED,
    )


def _reachability_snapshot() -> DeviceReachabilityData:
    """Real ``ReachabilityTracker.snapshot`` output — the highest-drift target.

    The tracker builds the dict literal as the source of truth
    for the wire shape; the TypedDict declaration mirrors it.
    Adding a freshness field on one side without the other would
    silently desync subscribers, so this factory exercises the
    actual snapshot path with a populated mDNS cache reader.
    """
    info = MdnsCacheInfo(
        age_seconds=12.4,
        ttl_remaining_seconds=107.6,
        txt_records={"version": "2025.5.0"},
    )
    tracker = ReachabilityTracker(mdns_cache_reader={"kitchen": info}.get)
    return tracker.snapshot(
        "kitchen",
        state=DeviceState.ONLINE,
        active_source="mdns",
        ip="10.0.0.42",
    )


# Each entry: (TypedDict, sample-payload factory). The factory must
# exercise the actual wire-shape construction site (TypedDict-call
# syntax, dict literal returned as a TypedDict, snapshot helper)
# rather than hand-rolling a dict — the whole point is to catch
# drift between the emitter and the model.
_PAYLOAD_FACTORIES: list[tuple[type, Any]] = [
    (
        DeviceEventData,
        lambda: DeviceEventData(device=_make_device()),
    ),
    (
        DeviceStateChangedData,
        lambda: DeviceStateChangedData(
            configuration="kitchen.yaml",
            state=DeviceState.ONLINE.value,
        ),
    ),
    (DeviceReachabilityData, _reachability_snapshot),
    (
        JobLifecycleData,
        lambda: JobLifecycleData(job=_make_job()),
    ),
    (
        JobOutputData,
        lambda: JobOutputData(job_id="abc123", line="hello\n"),
    ),
    (
        JobProgressData,
        lambda: JobProgressData(job_id="abc123", progress=42),
    ),
    (
        RemoteBuildPairRequestReceivedData,
        lambda: RemoteBuildPairRequestReceivedData(
            dashboard_id="peer-1",
            pin_sha256="0" * 64,
            label="laptop",
            peer_ip="10.0.0.99",
        ),
    ),
    (
        RemoteBuildPairStatusChangedData,
        lambda: RemoteBuildPairStatusChangedData(
            dashboard_id="peer-1",
            status="approved",
        ),
    ),
    (
        RemoteBuildPairingWindowChangedData,
        lambda: RemoteBuildPairingWindowChangedData(
            open=True,
            expires_in_seconds=300.0,
        ),
    ),
]


@pytest.mark.parametrize(
    ("typed_dict", "factory"),
    _PAYLOAD_FACTORIES,
    ids=[td.__name__ for td, _ in _PAYLOAD_FACTORIES],
)
def test_event_payload_keys_match_typeddict(
    typed_dict: type,
    factory: Any,
) -> None:
    """Every emitted payload's keys equal the TypedDict's declared fields.

    Pins the wire-shape contract at runtime. mypy already checks
    the TypedDict-call syntax at fire sites, but the
    ``ReachabilityTracker.snapshot`` path returns a dict-literal
    typed via the ``ReachabilitySnapshot = DeviceReachabilityData``
    alias — drift there would only surface as a runtime mismatch.
    Failing this test means a TypedDict field was added without
    updating the emitter (or the other way around).
    """
    payload = factory()
    declared = set(get_type_hints(typed_dict).keys())
    actual = set(payload.keys())

    extra = actual - declared
    missing = declared - actual
    assert not extra, f"{typed_dict.__name__} payload has unexpected keys: {extra}"
    assert not missing, f"{typed_dict.__name__} payload missing declared keys: {missing}"


def test_event_payload_factories_cover_every_event_data_typeddict() -> None:
    """Pin every ``*Data`` TypedDict to a row in ``_PAYLOAD_FACTORIES``.

    Without this gate, a future PR could land a new TypedDict and
    forget to add a factory — the param test would silently skip
    coverage for the new type. Walks every model module's
    namespace, finds anything matching the convention
    ``class XxxData(TypedDict)``, and verifies it's listed above.
    """
    discovered: set[str] = set()
    for name in dir(models_pkg):
        obj = getattr(models_pkg, name)
        # ``TypedDict`` declares a ``__total__`` attribute on its
        # subclasses (the runtime hook ``typing.TypedDict`` plants
        # for ``total=`` introspection); plain classes don't.
        if (
            isinstance(obj, type)
            and getattr(obj, "__total__", None) is not None
            and name.endswith("Data")
        ):
            discovered.add(name)

    covered = {td.__name__ for td, _ in _PAYLOAD_FACTORIES}
    uncovered = discovered - covered
    assert not uncovered, (
        f"TypedDicts without a payload factory: {sorted(uncovered)}. "
        "Add a factory to ``_PAYLOAD_FACTORIES`` so the contract "
        "test exercises the new shape."
    )
