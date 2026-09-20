"""Persistence adapter for the in-process local task runner."""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.models import LocalJobModel
from app.registry.constants import LocalJobStatus


class LocalJobRepository:
    """Database operations for durable local background jobs."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        task_name: str,
        payload: dict[str, Any],
        max_retries: int,
        retry_delay_seconds: int,
        timeout_seconds: int | None,
    ) -> UUID:
        """Create a job ready for immediate execution."""
        job = LocalJobModel(
            task_name=task_name,
            payload=payload,
            status=LocalJobStatus.PENDING,
            attempts=0,
            max_retries=max_retries,
            retry_delay_seconds=retry_delay_seconds,
            timeout_seconds=timeout_seconds,
            available_at=datetime.now(timezone.utc),
        )
        self._session.add(job)
        await self._session.flush()
        return job.id

    async def claim_next(self, now: datetime | None = None) -> LocalJobModel | None:
        """Atomically claim the oldest ready job, if one exists."""
        now = now or datetime.now(timezone.utc)
        stmt = (
            select(LocalJobModel)
            .where(
                LocalJobModel.status.in_([LocalJobStatus.PENDING, LocalJobStatus.RETRY]),
                LocalJobModel.available_at <= now,
            )
            .order_by(LocalJobModel.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        job = (await self._session.execute(stmt)).scalar_one_or_none()
        if job is None:
            return None

        job.status = LocalJobStatus.RUNNING
        job.attempts += 1
        job.started_at = now
        job.finished_at = None
        await self._session.commit()
        return job

    async def mark_completed(self, job_id: UUID, *, finished_at: datetime | None = None) -> None:
        """Mark a job as successfully completed."""
        await self._session.execute(
            update(LocalJobModel)
            .where(LocalJobModel.id == job_id)
            .values(
                status=LocalJobStatus.COMPLETED,
                finished_at=finished_at or datetime.now(timezone.utc),
                last_error=None,
            )
        )
        await self._session.commit()

    async def schedule_retry(
        self,
        job_id: UUID,
        *,
        available_at: datetime,
        error: str,
    ) -> None:
        """Move a failed attempt back to the retry queue."""
        await self._session.execute(
            update(LocalJobModel)
            .where(LocalJobModel.id == job_id)
            .values(
                status=LocalJobStatus.RETRY,
                available_at=available_at,
                last_error=error,
                finished_at=None,
            )
        )
        await self._session.commit()

    async def mark_failed(
        self,
        job_id: UUID,
        *,
        error: str,
        finished_at: datetime | None = None,
    ) -> None:
        """Mark a job as permanently failed."""
        await self._session.execute(
            update(LocalJobModel)
            .where(LocalJobModel.id == job_id)
            .values(
                status=LocalJobStatus.FAILED,
                last_error=error,
                finished_at=finished_at or datetime.now(timezone.utc),
            )
        )
        await self._session.commit()

    async def recover_running(self) -> int:
        """Return jobs left RUNNING by a previous application process to PENDING."""
        result = await self._session.execute(
            update(LocalJobModel)
            .where(LocalJobModel.status == LocalJobStatus.RUNNING)
            .values(
                status=LocalJobStatus.PENDING,
                available_at=datetime.now(timezone.utc),
                started_at=None,
                finished_at=None,
            )
        )
        await self._session.commit()
        return int(result.rowcount or 0)
