"""Coverage for the read-only firmware-job inspectors.

Two handlers, both pure read-throughs against ``self._jobs``:

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

from unittest.mock import AsyncMock

import pytest

from esphome_device_builder.controllers.firmware import FirmwareController
from esphome_device_builder.models import FirmwareJob, JobStatus, JobType


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


def _controller(*jobs: FirmwareJob) -> FirmwareController:
    """Bare-bones controller — ``get_jobs`` / ``get_job`` only read ``self._jobs``."""
    controller = FirmwareController.__new__(FirmwareController)
    controller._jobs = {j.job_id: j for j in jobs}
    # Stub the bits ``__init__`` would have set so debug logging /
    # unrelated paths don't ``AttributeError`` if a future
    # refactor touches them inside the inspectors.
    controller._persist_jobs = AsyncMock()
    return controller


# ---------------------------------------------------------------------------
# get_jobs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_jobs_returns_every_job_when_unfiltered() -> None:
    """No filters → every job in the map is returned.

    The all-jobs panel calls this on cold-start to populate the
    list before subscribing to the event stream; a regression
    that silently dropped any subset of jobs would leave rows
    missing on first paint.
    """
    a = _job("a")
    b = _job("b")
    c = _job("c")
    controller = _controller(a, b, c)

    result = await controller.get_jobs()

    assert {j.job_id for j in result} == {"a", "b", "c"}


@pytest.mark.asyncio
async def test_get_jobs_sorts_newest_first_by_created_at() -> None:
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
    controller = _controller(middle, old, new)

    result = await controller.get_jobs()

    assert [j.job_id for j in result] == ["new", "middle", "old"]


@pytest.mark.asyncio
async def test_get_jobs_filters_by_status() -> None:
    """``status`` filter keeps only jobs whose status matches.

    Frontend uses this to render the "Recently completed" panel
    (``status=COMPLETED``) without having to download the full
    history client-side.
    """
    queued = _job("q", status=JobStatus.QUEUED)
    running = _job("r", status=JobStatus.RUNNING)
    completed = _job("c", status=JobStatus.COMPLETED)
    controller = _controller(queued, running, completed)

    result = await controller.get_jobs(status=JobStatus.COMPLETED)

    assert result == [completed]


@pytest.mark.asyncio
async def test_get_jobs_filters_by_configuration() -> None:
    """``configuration`` filter keeps only jobs for that YAML."""
    kitchen = _job("k", configuration="kitchen.yaml")
    garage = _job("g", configuration="garage.yaml")
    office = _job("o", configuration="office.yaml")
    controller = _controller(kitchen, garage, office)

    result = await controller.get_jobs(configuration="garage.yaml")

    assert result == [garage]


@pytest.mark.asyncio
async def test_get_jobs_combines_status_and_configuration_filters() -> None:
    """Both filters compose with AND semantics."""
    kitchen_queued = _job("kq", configuration="kitchen.yaml", status=JobStatus.QUEUED)
    kitchen_done = _job("kd", configuration="kitchen.yaml", status=JobStatus.COMPLETED)
    garage_done = _job("gd", configuration="garage.yaml", status=JobStatus.COMPLETED)
    controller = _controller(kitchen_queued, kitchen_done, garage_done)

    result = await controller.get_jobs(configuration="kitchen.yaml", status=JobStatus.COMPLETED)

    assert result == [kitchen_done]


@pytest.mark.asyncio
async def test_get_jobs_filter_with_no_matches_returns_empty_list() -> None:
    """A filter that matches nothing returns ``[]``, not a raise.

    Distinct from ``get_job`` (which returns ``None`` for an
    unknown id): ``get_jobs`` is list-shaped, so the empty case
    is the empty list. The frontend renders an empty list as
    "no jobs match"; raising would force every caller to add a
    try/except for a perfectly valid query.
    """
    controller = _controller(_job("a", status=JobStatus.COMPLETED))

    result = await controller.get_jobs(status=JobStatus.RUNNING)

    assert result == []


@pytest.mark.asyncio
async def test_get_jobs_on_empty_controller_returns_empty_list() -> None:
    """An empty job map → empty list (cold-start contract)."""
    controller = _controller()

    assert await controller.get_jobs() == []


# ---------------------------------------------------------------------------
# get_job
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_job_returns_the_matching_job_for_known_id() -> None:
    """Known id → the ``FirmwareJob`` instance, full object including ``output``.

    Frontend uses this to fetch the full output buffer when the
    user clicks into a job's detail view; keeping the full
    object means no extra round-trip for the output.
    """
    target = _job("target")
    other = _job("other")
    controller = _controller(target, other)

    result = await controller.get_job(job_id="target")

    assert result is target


@pytest.mark.asyncio
async def test_get_job_returns_none_for_unknown_id() -> None:
    """Unknown id → ``None``, NOT a raise.

    Distinct from ``cancel`` and ``follow_job``, which both
    raise on unknown ids. ``get_job`` is the explicit "look up
    by id, fall back to None" path — frontend uses it to ask
    "is this job still tracked?" without having to handle an
    exception for the negative answer.
    """
    controller = _controller(_job("present"))

    assert await controller.get_job(job_id="ghost") is None


@pytest.mark.asyncio
async def test_get_job_does_not_mutate_state() -> None:
    """Pure read — the call doesn't mutate ``self._jobs`` or persist anything.

    Belt-and-braces: a future refactor that, say, lazy-removes
    terminal jobs on read would silently change the contract for
    every caller. Pin the read-only nature so a refactor showing
    up here forces a docs / migration discussion.
    """
    target = _job("target")
    controller = _controller(target)
    before = dict(controller._jobs)

    await controller.get_job(job_id="target")

    assert controller._jobs == before
    controller._persist_jobs.assert_not_awaited()
