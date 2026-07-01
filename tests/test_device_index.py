"""Direct unit tests for ``_DeviceIndex``.

The index is the structural enforcement of the lockstep
invariant the scanner used to rely on convention for: every
mutation runs through ``set`` / ``pop`` / ``rebuild_in_path_order``
and updates the path-keyed dict, the name-keyed buckets, and
the cache-key dict together. Testing it in isolation means a
regression in the lockstep surfaces here, not as a downstream
mDNS-fanout flake when the scanner happens to take the
affected code path.

The scanner-level tests in ``test_device_scanner_order.py`` and
``test_device_scanner_branches.py`` already drive the index
through realistic flows. These tests round out coverage by
pinning the per-method invariants without spinning up a scanner
or hitting disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from esphome_device_builder.controllers._device_scanner import _DeviceIndex
from esphome_device_builder.models import Device

from .conftest import make_device


def _device(name: str, configuration: str, *, friendly_name: str | None = None) -> Device:
    return make_device(
        name=name,
        friendly_name=friendly_name or name.title(),
        configuration=configuration,
        address="",
    )


# ---------------------------------------------------------------------------
# set — happy paths and lockstep
# ---------------------------------------------------------------------------


def test_set_inserts_new_device_into_every_map() -> None:
    """A first ``set`` lands the device in path / name / cache-key maps together."""
    index = _DeviceIndex()
    path = Path("/cfg/kitchen.yaml")
    device = _device("kitchen", "kitchen.yaml")

    index.set(path, device, (1, 2, 3.0, 4))

    assert index.by_path == {path: device}
    assert index.devices == [device]
    assert index.get_by_name("kitchen") == [device]
    assert index.cache_key(path) == (1, 2, 3.0, 4)


def test_set_updates_existing_path_in_place() -> None:
    """Re-``set``-ing the same path replaces the device and its cache key.

    Tracks the YAML-touched-but-same-name flow: ``previous`` is
    found, removed from the bucket, and the fresh Device takes
    its sorted position. Cache key updates so the next scan
    re-evaluates only on a real change.
    """
    index = _DeviceIndex()
    path = Path("/cfg/kitchen.yaml")
    stale = _device("kitchen", "kitchen.yaml", friendly_name="Stale")
    fresh = _device("kitchen", "kitchen.yaml", friendly_name="Fresh")

    index.set(path, stale, (0, 0, 0.0, 0))
    index.set(path, fresh, (1, 2, 3.0, 4))

    assert index.by_path[path] is fresh
    assert index.get_by_name("kitchen") == [fresh]
    assert index.cache_key(path) == (1, 2, 3.0, 4)


def test_set_handles_yaml_rename_by_rebucketing() -> None:
    """A rename (``previous.name != device.name``) drops the old bucket entry.

    Without the unindex, mDNS lookups under the new name would
    miss while the old name's bucket would fan out to a stale
    Device — the symptom that motivated the fan-out work in the
    first place.
    """
    index = _DeviceIndex()
    path = Path("/cfg/device.yaml")
    old = _device("kitchen", "device.yaml")
    new = _device("lounge", "device.yaml")

    index.set(path, old, (0, 0, 0.0, 0))
    index.set(path, new, (1, 1, 1.0, 1))

    assert index.get_by_name("kitchen") == []
    assert index.get_by_name("lounge") == [new]
    # Path-keyed map keeps the same key with the new Device.
    assert index.by_path == {path: new}


def test_set_keeps_bucket_sorted_by_configuration_filename() -> None:
    """Two devices sharing a name land in lexicographic ``configuration`` order.

    ``bucket[0]`` consumers (the apply / dedupe path) need a
    deterministic "first match" so dedupe doesn't flip-flop on
    duplicate-named YAMLs across scans. Insert order doesn't
    matter — the position is computed from ``configuration``.
    """
    index = _DeviceIndex()
    # Insert in non-alphabetical order to prove the sort is real.
    index.set(Path("/cfg/zebra.yaml"), _device("dup", "zebra.yaml"), (0, 0, 0.0, 0))
    index.set(Path("/cfg/alpha.yaml"), _device("dup", "alpha.yaml"), (0, 0, 0.0, 0))
    index.set(Path("/cfg/mike.yaml"), _device("dup", "mike.yaml"), (0, 0, 0.0, 0))

    bucket = index.get_by_name("dup")
    assert [d.configuration for d in bucket] == ["alpha.yaml", "mike.yaml", "zebra.yaml"]


def test_get_by_name_returns_a_snapshot() -> None:
    """The returned list is a copy — caller mutation can't poison the index.

    Mirrors the public ``DeviceScanner.get_by_name`` semantic: a
    careless ``.clear()`` or ``.append()`` on the returned bucket
    must not silently break the next mDNS announcement's
    fan-out.
    """
    index = _DeviceIndex()
    device = _device("kitchen", "kitchen.yaml")
    index.set(Path("/cfg/kitchen.yaml"), device, (0, 0, 0.0, 0))

    bucket = index.get_by_name("kitchen")
    bucket.clear()
    bucket.append(_device("ghost", "ghost.yaml"))

    fresh = index.get_by_name("kitchen")
    assert fresh == [device]


def test_get_by_name_returns_empty_for_unknown_name() -> None:
    """An unknown name yields ``[]``, not ``None``, so callers can iterate."""
    index = _DeviceIndex()
    assert index.get_by_name("never-seen") == []


# ---------------------------------------------------------------------------
# pop — lockstep removal
# ---------------------------------------------------------------------------


def test_pop_removes_from_every_map_and_returns_device() -> None:
    """Popping a tracked path clears it from path / name / cache-key dicts."""
    index = _DeviceIndex()
    path = Path("/cfg/kitchen.yaml")
    device = _device("kitchen", "kitchen.yaml")
    index.set(path, device, (1, 2, 3.0, 4))

    popped = index.pop(path)

    assert popped is device
    assert dict(index.by_path) == {}
    assert index.get_by_name("kitchen") == []
    with pytest.raises(KeyError):
        index.cache_key(path)


def test_pop_returns_none_for_unknown_path() -> None:
    """Popping a path the index never knew about is a silent no-op."""
    index = _DeviceIndex()
    assert index.pop(Path("/cfg/ghost.yaml")) is None


def test_pop_leaves_sibling_in_shared_name_bucket() -> None:
    """Removing one of two name-sharing devices keeps the sibling indexed.

    The ``foo (1).yaml`` / ``dashboard_import`` siblings case —
    pin that the bucket survives partial removal and ``get_by_name``
    still returns the survivor.
    """
    index = _DeviceIndex()
    a = _device("kitchen", "a.yaml")
    b = _device("kitchen", "b.yaml")
    index.set(Path("/cfg/a.yaml"), a, (0, 0, 0.0, 0))
    index.set(Path("/cfg/b.yaml"), b, (0, 0, 0.0, 0))

    index.pop(Path("/cfg/a.yaml"))

    assert index.get_by_name("kitchen") == [b]


# ---------------------------------------------------------------------------
# find_path_by_filename / cache_key — read accessors
# ---------------------------------------------------------------------------


def test_find_path_by_filename_returns_matching_path() -> None:
    """``find_path_by_filename`` matches by ``Path.name``, not full path string."""
    index = _DeviceIndex()
    path = Path("/cfg/kitchen.yaml")
    index.set(path, _device("kitchen", "kitchen.yaml"), (0, 0, 0.0, 0))

    assert index.find_path_by_filename("kitchen.yaml") == path


def test_find_path_by_filename_returns_none_when_missing() -> None:
    """No match returns ``None`` — the caller treats it as "not tracked"."""
    index = _DeviceIndex()
    assert index.find_path_by_filename("ghost.yaml") is None


# ---------------------------------------------------------------------------
# get_by_configuration — configuration-keyed lookup (lockstep)
# ---------------------------------------------------------------------------


def test_get_by_configuration_returns_the_device() -> None:
    """``set`` makes the device resolvable by its ``configuration`` filename."""
    index = _DeviceIndex()
    device = _device("kitchen", "kitchen.yaml")
    index.set(Path("/cfg/kitchen.yaml"), device, (0, 0, 0.0, 0))

    assert index.get_by_configuration("kitchen.yaml") is device


def test_get_by_configuration_returns_none_for_unknown() -> None:
    """An untracked filename yields ``None``."""
    index = _DeviceIndex()
    assert index.get_by_configuration("ghost.yaml") is None


def test_get_by_configuration_follows_in_place_update() -> None:
    """Re-``set``-ing the same path returns the fresh Device, not the stale one."""
    index = _DeviceIndex()
    path = Path("/cfg/kitchen.yaml")
    stale = _device("kitchen", "kitchen.yaml", friendly_name="Stale")
    fresh = _device("kitchen", "kitchen.yaml", friendly_name="Fresh")
    index.set(path, stale, (0, 0, 0.0, 0))
    index.set(path, fresh, (1, 2, 3.0, 4))

    assert index.get_by_configuration("kitchen.yaml") is fresh


def test_get_by_configuration_survives_name_change() -> None:
    """A same-file ``esphome.name`` edit keeps the configuration lookup valid."""
    index = _DeviceIndex()
    path = Path("/cfg/device.yaml")
    index.set(path, _device("kitchen", "device.yaml"), (0, 0, 0.0, 0))
    renamed = _device("lounge", "device.yaml")
    index.set(path, renamed, (1, 1, 1.0, 1))

    assert index.get_by_configuration("device.yaml") is renamed


def test_get_by_configuration_returns_none_after_pop() -> None:
    """Popping the path drops the configuration entry too."""
    index = _DeviceIndex()
    path = Path("/cfg/kitchen.yaml")
    index.set(path, _device("kitchen", "kitchen.yaml"), (0, 0, 0.0, 0))
    index.pop(path)

    assert index.get_by_configuration("kitchen.yaml") is None


def test_get_by_configuration_distinguishes_name_sharing_siblings() -> None:
    """Two YAMLs sharing a ``name`` resolve independently by filename."""
    index = _DeviceIndex()
    a = _device("kitchen", "a.yaml")
    b = _device("kitchen", "b.yaml")
    index.set(Path("/cfg/a.yaml"), a, (0, 0, 0.0, 0))
    index.set(Path("/cfg/b.yaml"), b, (0, 0, 0.0, 0))

    assert index.get_by_configuration("a.yaml") is a
    assert index.get_by_configuration("b.yaml") is b


# ---------------------------------------------------------------------------
# rebuild_in_path_order — lexicographic re-key
# ---------------------------------------------------------------------------


def test_rebuild_in_path_order_re_keys_devices_and_cache_keys() -> None:
    """Iteration order over ``devices`` follows the supplied path order.

    The scanner's ``_do_scan`` re-keys the index in sorted-path
    order after every add/remove so the dashboard's device list
    is stable across restarts. Pin that both ``_devices`` and
    ``_cache_keys`` follow the new order — drift between the two
    would silently break change detection on the next scan.
    """
    index = _DeviceIndex()
    paths = [Path(f"/cfg/{n}.yaml") for n in ("zebra", "alpha", "mike")]
    for p in paths:
        index.set(p, _device(p.stem, p.name), (0, 0, 0.0, 0))

    # Apply lexicographic order — opposite of insertion order.
    index.rebuild_in_path_order(sorted(paths))

    # by_path is a live view; iteration order matches the rebuild.
    assert list(index.by_path.keys()) == sorted(paths)
    # ``devices`` follows the re-keyed iteration order.
    assert [d.name for d in index.devices] == ["alpha", "mike", "zebra"]


def test_rebuild_in_path_order_ignores_extra_paths_never_indexed() -> None:
    """Extra paths in *path_order* that aren't in the index are silently filtered.

    A YAML that failed to load (skipped + logged in
    ``_load_devices``) sits in the scanner's ``path_to_cache_key``
    but never made it into the index. ``rebuild_in_path_order``
    is called with ``path_to_cache_key.keys()`` so it has to
    tolerate those phantom paths without ``KeyError``-ing.
    """
    index = _DeviceIndex()
    a = Path("/cfg/a.yaml")
    b = Path("/cfg/b.yaml")
    c = Path("/cfg/c.yaml")  # never .set() — simulates failed load
    index.set(a, _device("a", "a.yaml"), (0, 0, 0.0, 0))
    index.set(b, _device("b", "b.yaml"), (0, 0, 0.0, 0))

    index.rebuild_in_path_order([a, b, c])

    assert list(index.by_path.keys()) == [a, b]
    with pytest.raises(KeyError):
        index.cache_key(c)


def test_rebuild_in_path_order_rejects_omitting_indexed_paths() -> None:
    """A *path_order* that omits a tracked path raises ``ValueError``.

    Silently dropping a path from ``_devices`` while leaving its
    Device in the name buckets would break the lockstep
    invariant. The scanner's ``_do_scan`` already pops removed
    paths via ``pop()`` *before* calling ``rebuild_in_path_order``,
    so this assertion holds in practice; pin the structural
    refusal so a future caller that forgets to pop first surfaces
    immediately rather than as an mDNS-fanout flake later.
    """
    index = _DeviceIndex()
    a = Path("/cfg/a.yaml")
    b = Path("/cfg/b.yaml")
    index.set(a, _device("a", "a.yaml"), (0, 0, 0.0, 0))
    index.set(b, _device("b", "b.yaml"), (0, 0, 0.0, 0))

    with pytest.raises(ValueError, match=r"missing 1 indexed path"):
        index.rebuild_in_path_order([a])  # omits b — must refuse


# ---------------------------------------------------------------------------
# by_path read-only view contract
# ---------------------------------------------------------------------------


def test_by_path_view_rejects_mutation() -> None:
    """``by_path`` is a ``MappingProxyType`` — direct mutation raises.

    Returning the underlying ``dict`` would let a careless caller
    bypass the lockstep mutation API by writing to the path index
    directly (which would leave the name buckets / cache keys
    stale). Pin that the view physically rejects mutation so a
    refactor that handed back the bare dict surfaces here.
    """
    index = _DeviceIndex()
    path = Path("/cfg/kitchen.yaml")
    index.set(path, _device("kitchen", "kitchen.yaml"), (0, 0, 0.0, 0))

    by_path = index.by_path
    with pytest.raises(TypeError):
        by_path[Path("/cfg/ghost.yaml")] = _device("ghost", "ghost.yaml")  # type: ignore[index]
    with pytest.raises(TypeError):
        del by_path[path]  # type: ignore[attr-defined]


def test_by_path_view_reflects_subsequent_mutations() -> None:
    """The proxy is *live* — additions / removals show up immediately.

    A caller that captures ``by_path`` once and re-reads later
    expects to see the current state, not a frozen snapshot.
    Pin both add and remove flows.
    """
    index = _DeviceIndex()
    by_path = index.by_path
    assert dict(by_path) == {}

    path = Path("/cfg/kitchen.yaml")
    index.set(path, _device("kitchen", "kitchen.yaml"), (0, 0, 0.0, 0))
    assert path in by_path
    assert by_path[path].name == "kitchen"

    index.pop(path)
    assert path not in by_path


def test_by_path_reflects_rebuild_path_order() -> None:
    """A fresh ``by_path`` read after ``rebuild_in_path_order`` reflects the new order.

    ``rebuild_in_path_order`` rebinds ``_devices`` to a freshly
    re-keyed dict; the ``by_path`` property creates a new
    ``MappingProxyType`` wrapping the current dict on each call,
    so a post-rebuild read sees the new iteration order. (A
    proxy *captured* before the rebuild would point at the
    discarded dict — callers shouldn't cache the view across
    rebuilds.)
    """
    index = _DeviceIndex()
    paths = [Path(f"/cfg/{n}.yaml") for n in ("zebra", "alpha")]
    for p in paths:
        index.set(p, _device(p.stem, p.name), (0, 0, 0.0, 0))

    index.rebuild_in_path_order(sorted(paths))

    # Fresh read sees the new order.
    assert list(index.by_path.keys()) == sorted(paths)
    # And ``devices`` (also a fresh snapshot off the same dict) agrees.
    assert [d.name for d in index.devices] == ["alpha", "zebra"]
