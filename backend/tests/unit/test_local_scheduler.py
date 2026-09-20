"""Unit tests for local periodic scheduling and duplicate suppression."""

import asyncio
from uuid import UUID, uuid4

import pytest

from app.infrastructure.local_tasks import queue as queue_module
from app.infrastructure.local_tasks import scheduler as scheduler_module


@pytest.mark.asyncio
async def test_scheduler_enqueues_after_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """A schedule should enqueue a registered job after its interval."""
    calls: list[tuple[str, dict[str, object], bool]] = []

    async def fake_enqueue(
        task_name: str,
        payload: dict[str, object],
        *,
        only_if_absent: bool,
    ) -> UUID:
        calls.append((task_name, payload, only_if_absent))
        return uuid4()

    monkeypatch.setattr(scheduler_module, "enqueue_registered_job", fake_enqueue)
    scheduler = scheduler_module.LocalScheduler(
        (
            scheduler_module.PeriodicTask(
                name="test",
                task_name="check_eval_monitors",
                interval_seconds=0.01,
            ),
        )
    )

    await scheduler.start()
    await asyncio.sleep(0.03)
    await scheduler.stop()

    assert calls
    assert calls[0] == ("check_eval_monitors", {}, True)


@pytest.mark.asyncio
async def test_registered_job_skips_active_task_and_allows_new_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only active jobs are suppressed; after completion a new job is allowed."""
    session = _FakeSession()

    class FakeRepository:
        active = True

        def __init__(self, _session: object) -> None:
            self.created: list[dict[str, object]] = []

        async def has_active_task(self, _task_name: str) -> bool:
            return self.active

        async def create(self, **kwargs: object) -> UUID:
            self.created.append(kwargs)
            return UUID("00000000-0000-4000-a000-000000000123")

    repository = FakeRepository(session)
    monkeypatch.setattr(queue_module, "async_session_factory", lambda: session)
    monkeypatch.setattr(queue_module, "LocalJobRepository", lambda _session: repository)

    assert (
        await queue_module.enqueue_registered_job(
            "dispatch_sync_usage",
            {},
            only_if_absent=True,
        )
        is None
    )
    assert session.commits == 0

    repository.active = False
    job_id = await queue_module.enqueue_registered_job(
        "dispatch_sync_usage",
        {},
        only_if_absent=True,
    )

    assert job_id == UUID("00000000-0000-4000-a000-000000000123")
    assert session.commits == 1
    assert repository.created[0] == {
        "task_name": "dispatch_sync_usage",
        "payload": {},
        "max_retries": 0,
        "retry_delay_seconds": 0,
        "timeout_seconds": 300,
    }


class _FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1
