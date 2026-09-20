"""Queue helpers for durable jobs executed by the FastAPI process."""

from typing import Any
from uuid import UUID

from app.infrastructure.db.engine import async_session_factory
from app.infrastructure.db.repositories.local_job_repo import LocalJobRepository


async def enqueue_local_job(
    *,
    task_name: str,
    payload: dict[str, Any],
    max_retries: int,
    retry_delay_seconds: int,
    timeout_seconds: int | None,
) -> UUID:
    """Persist a local job and return its durable identifier."""
    async with async_session_factory() as session:
        repo = LocalJobRepository(session)
        job_id = await repo.create(
            task_name=task_name,
            payload=payload,
            max_retries=max_retries,
            retry_delay_seconds=retry_delay_seconds,
            timeout_seconds=timeout_seconds,
        )
        await session.commit()
        return job_id


async def enqueue_registered_job(
    task_name: str,
    payload: dict[str, Any],
    *,
    only_if_absent: bool = False,
) -> UUID | None:
    """Enqueue a task using the configuration from ``TASK_DEFINITIONS``."""
    from app.infrastructure.local_tasks.definitions import TASK_DEFINITIONS

    definition = TASK_DEFINITIONS.get(task_name)
    if definition is None:
        raise ValueError(f"Unknown local task: {task_name}")

    async with async_session_factory() as session:
        repo = LocalJobRepository(session)
        if only_if_absent and await repo.has_active_task(task_name):
            return None

        job_id = await repo.create(
            task_name=task_name,
            payload=payload,
            max_retries=definition.max_retries,
            retry_delay_seconds=definition.retry_delay_seconds,
            timeout_seconds=definition.timeout_seconds,
        )
        await session.commit()
        return job_id
