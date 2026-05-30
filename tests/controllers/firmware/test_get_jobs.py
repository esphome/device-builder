"""Coverage for the read-only firmware-job inspectors.

Two handlers, both pure read-throughs against ``self.state.jobs``:

- ``firmware/get_jobs`` — filtered + sorted listing. Filters by
  ``status`` and ``configuration``; sorts newest-first by
  ``created_at``. Either filter can be omitted; both can be
  passed together.
- ``firmware/get_job`` — single-job lookup by id. Returns the
  job or ``None``; never raises (in contrast to ``cancel`` and
  ``follow_job`` which raise on unknown ids).

Both are simple but the contract details (sort direction, the
two filters being independently optional, the unknown-id
``None`` return) are easy to flip in a refactor without anyone
noticing.
"""

from __future__ import annotations

from esphome_device_builder.models import FirmwareJob, JobStatus, JobType
from tests.controllers.firmware.conftest import FirmwareControllerFactory


def _job(
    job_id: str,
    *,
    configuration: str = "kitchen.yaml",
    status: JobStatus = JobStatus.QUEUED,
    job_type: JobType = JobType.COMPILE,
    created_at: str = "2026-01-01T00:00:00+00:00",
) -> FirmwareJob:
    return FirmwareJob(
        job_id=job_id,
        configuration=configuration,
        job_type=job_type,
        status=status,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# get_jobs
# ---------------------------------------------------------------------------


async def test_get_jobs_returns_every_job_when_unfiltered(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """No filters → every job in the map is returned.

    The all-jobs panel calls this on cold-start to populate the
    list before subscribing to the event stream; a regression
    that silently dropped any subset of jobs would leave rows
    missing on first paint.
    """
    a = _job("a")
    b = _job("b")
    c = _job("c")
    controller = firmware_controller_factory(a, b, c, with_settings=False)

    result = await controller.get_jobs()

    assert {j.job_id for j in result} == {"a", "b", "c"}


async def test_get_jobs_sorts_newest_first_by_created_at(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Result is sorted by ``created_at`` descending (newest first).

    The dashboard renders the list top-down; newest at the top
    is the operator's expected reading order. Pin the sort
    direction so a refactor that flips ``reverse=True`` to
    ``False`` (or sorts on a different field) shows up here.
    """
    old = _job("old", created_at="2026-01-01T00:00:00+00:00")
    middle = _job("middle", created_at="2026-01-02T00:00:00+00:00")
    new = _job("new", created_at="2026-01-03T00:00:00+00:00")
    # Insert order intentionally not-sorted so the test catches
    # "returns dict insertion order" as a false positive.
    controller = firmware_controller_factory(middle, old, new, with_settings=False)

    result = await controller.get_jobs()

    assert [j.job_id for j in result] == ["new", "middle", "old"]


async def test_get_jobs_filters_by_status(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """``status`` filter keeps only jobs whose status matches.

    Frontend uses this to render the "Recently completed" panel
    (``status=COMPLETED``) without having to download the full
    history client-side.
    """
    queued = _job("q", status=JobStatus.QUEUED)
    running = _job("r", status=JobStatus.RUNNING)
    completed = _job("c", status=JobStatus.COMPLETED)
    controller = firmware_controller_factory(queued, running, completed, with_settings=False)

    result = await controller.get_jobs(status=JobStatus.COMPLETED)

    assert result == [completed]


async def test_get_jobs_filters_by_configuration(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """``configuration`` filter keeps only jobs for that YAML."""
    kitchen = _job("k", configuration="kitchen.yaml")
    garage = _job("g", configuration="garage.yaml")
    office = _job("o", configuration="office.yaml")
    controller = firmware_controller_factory(kitchen, garage, office, with_settings=False)

    result = await controller.get_jobs(configuration="garage.yaml")

    assert result == [garage]


async def test_get_jobs_combines_status_and_configuration_filters(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Both filters compose with AND semantics."""
    kitchen_queued = _job("kq", configuration="kitchen.yaml", status=JobStatus.QUEUED)
    kitchen_done = _job("kd", configuration="kitchen.yaml", status=JobStatus.COMPLETED)
    garage_done = _job("gd", configuration="garage.yaml", status=JobStatus.COMPLETED)
    controller = firmware_controller_factory(
        kitchen_queued, kitchen_done, garage_done, with_settings=False
    )

    result = await controller.get_jobs(configuration="kitchen.yaml", status=JobStatus.COMPLETED)

    assert result == [kitchen_done]


async def test_get_jobs_filter_with_no_matches_returns_empty_list(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A filter that matches nothing returns ``[]``, not a raise.

    Distinct from ``get_job`` (which returns ``None`` for an
    unknown id): ``get_jobs`` is list-shaped, so the empty case
    is the empty list. The frontend renders an empty list as
    "no jobs match"; raising would force every caller to add a
    try/except for a perfectly valid query.
    """
    controller = firmware_controller_factory(
        _job("a", status=JobStatus.COMPLETED), with_settings=False
    )

    result = await controller.get_jobs(status=JobStatus.RUNNING)

    assert result == []


async def test_get_jobs_on_empty_controller_returns_empty_list(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """An empty job map → empty list (cold-start contract)."""
    controller = firmware_controller_factory(with_settings=False)

    assert await controller.get_jobs() == []


# ---------------------------------------------------------------------------
# get_job
# ---------------------------------------------------------------------------


async def test_get_job_returns_the_matching_job_for_known_id(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Known id → the ``FirmwareJob`` instance, full object including ``output``.

    Frontend uses this to fetch the full output buffer when the
    user clicks into a job's detail view; keeping the full
    object means no extra round-trip for the output.
    """
    target = _job("target")
    other = _job("other")
    controller = firmware_controller_factory(target, other, with_settings=False)

    result = await controller.get_job(job_id="target")

    assert result is target


async def test_get_job_returns_none_for_unknown_id(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Unknown id → ``None``, NOT a raise.

    Distinct from ``cancel`` and ``follow_job``, which both
    raise on unknown ids. ``get_job`` is the explicit "look up
    by id, fall back to None" path — frontend uses it to ask
    "is this job still tracked?" without having to handle an
    exception for the negative answer.
    """
    controller = firmware_controller_factory(_job("present"), with_settings=False)

    assert await controller.get_job(job_id="ghost") is None


async def test_get_job_does_not_mutate_state(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Pure read — the call doesn't add/remove jobs or persist anything.

    Belt-and-braces: a future refactor that, say, lazy-removes
    terminal jobs on read would silently change the contract for
    every caller. Pin the read-only nature so a refactor showing
    up here forces a docs / migration discussion.
    """
    target = _job("target")
    controller = firmware_controller_factory(target, with_settings=False)
    before = await controller.get_jobs()

    await controller.get_job(job_id="target")

    assert await controller.get_jobs() == before
    controller._persist_jobs.assert_not_awaited()


# ---------------------------------------------------------------------------
# active_remote_peer_jobs
# ---------------------------------------------------------------------------


def test_active_remote_peer_jobs_yields_only_in_flight_remote_jobs(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Only QUEUED / RUNNING jobs with non-empty ``remote_peer`` are yielded.

    The 6c cleanup sweep keys off this iterator to skip
    in-flight subtrees; other potential callers (future
    schedulers, diagnostics surfaces) need the same shape.
    Pin every filter branch so a future refactor that
    inverts a condition trips here instead of in production.
    """
    local_queued = _job("local-queued", status=JobStatus.QUEUED)
    remote_queued = FirmwareJob(
        job_id="remote-queued",
        configuration=".esphome/.remote_builds/alpha/kitchen/kitchen.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.QUEUED,
        remote_peer="alpha",
    )
    remote_running = FirmwareJob(
        job_id="remote-running",
        configuration=".esphome/.remote_builds/alpha/bedroom/bedroom.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.RUNNING,
        remote_peer="alpha",
    )
    remote_completed = FirmwareJob(
        job_id="remote-completed",
        configuration=".esphome/.remote_builds/alpha/bath/bath.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.COMPLETED,
        remote_peer="alpha",
    )
    controller = firmware_controller_factory(
        local_queued, remote_queued, remote_running, remote_completed, with_settings=False
    )

    yielded = list(controller.active_remote_peer_jobs())

    assert {job.job_id for job in yielded} == {"remote-queued", "remote-running"}


def test_active_remote_peer_jobs_empty_when_no_remote_jobs(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """All-local jobs → empty iterator; the cleanup sweep gets an empty in-flight set."""
    controller = firmware_controller_factory(
        _job("local-1", status=JobStatus.QUEUED),
        _job("local-2", status=JobStatus.RUNNING),
        with_settings=False,
    )

    assert list(controller.active_remote_peer_jobs()) == []


# ---------------------------------------------------------------------------
# find_remote_peer_job / remote_peer_job_ids
# ---------------------------------------------------------------------------


def _remote_job(
    job_id: str,
    *,
    remote_peer: str,
    remote_job_id: str,
    status: JobStatus = JobStatus.QUEUED,
) -> FirmwareJob:
    return FirmwareJob(
        job_id=job_id,
        configuration=f".esphome/.remote_builds/{remote_peer}/{job_id}/{job_id}.yaml",
        job_type=JobType.COMPILE,
        status=status,
        remote_peer=remote_peer,
        remote_job_id=remote_job_id,
    )


def test_find_remote_peer_job_returns_matching_job(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Matching (remote_peer, remote_job_id) → the FirmwareJob instance.

    The receiver's artifacts-download path uses this as the
    single lookup that gates "is this offloader allowed to
    fetch artifacts for this job?". Pin the positive match
    against a peer/id pair that also has a sibling job under
    the same peer (different id) so a regression that returned
    the first peer match wouldn't pass.
    """
    target = _remote_job("target", remote_peer="alpha", remote_job_id="r-1")
    sibling = _remote_job("sibling", remote_peer="alpha", remote_job_id="r-2")
    controller = firmware_controller_factory(target, sibling, with_settings=False)

    result = controller.find_remote_peer_job(remote_peer="alpha", remote_job_id="r-1")

    assert result is target


def test_find_remote_peer_job_returns_none_when_remote_job_id_mismatches(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Right peer, wrong remote_job_id → None.

    Both fields must match — the receiver path uses this to
    reject artifacts requests for an id the peer never
    submitted, so the per-field check has to be AND, not OR.
    """
    job = _remote_job("present", remote_peer="alpha", remote_job_id="r-1")
    controller = firmware_controller_factory(job, with_settings=False)

    result = controller.find_remote_peer_job(remote_peer="alpha", remote_job_id="r-ghost")

    assert result is None


def test_find_remote_peer_job_returns_none_when_state_is_empty(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Empty state.jobs → None, no raise.

    Cold-start contract: the receiver may query before any job
    has landed. Linear scan over an empty dict returns None
    cleanly.
    """
    controller = firmware_controller_factory(with_settings=False)

    assert controller.find_remote_peer_job(remote_peer="alpha", remote_job_id="r-1") is None


def test_remote_peer_job_ids_returns_remote_job_ids_for_peer(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Every job under *remote_peer* contributes its ``remote_job_id``.

    The artifacts-download reject log uses this list to surface
    "available ids" when a requested one doesn't match. Pin a
    two-id case so a regression that returned only the first
    match would show up here.
    """
    a = _remote_job("alpha-a", remote_peer="alpha", remote_job_id="r-a")
    b = _remote_job("alpha-b", remote_peer="alpha", remote_job_id="r-b")
    controller = firmware_controller_factory(a, b, with_settings=False)

    assert sorted(controller.remote_peer_job_ids(remote_peer="alpha")) == ["r-a", "r-b"]


def test_remote_peer_job_ids_excludes_jobs_from_other_peers(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Jobs whose ``remote_peer`` doesn't match are filtered out.

    Receivers serve multiple offloaders simultaneously; the
    reject log mustn't leak ids from a different peer's
    queue.
    """
    mine = _remote_job("mine", remote_peer="alpha", remote_job_id="r-mine")
    theirs = _remote_job("theirs", remote_peer="beta", remote_job_id="r-theirs")
    controller = firmware_controller_factory(mine, theirs, with_settings=False)

    assert controller.remote_peer_job_ids(remote_peer="alpha") == ["r-mine"]


def test_remote_peer_job_ids_returns_empty_list_when_state_is_empty(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Empty state.jobs → empty list, not None or a raise."""
    controller = firmware_controller_factory(with_settings=False)

    assert controller.remote_peer_job_ids(remote_peer="alpha") == []
