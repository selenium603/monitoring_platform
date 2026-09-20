"""Durable single-concurrency background runner for the FastAPI process."""

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID

from app.infrastructure.db.engine import async_session_factory
from app.infrastructure.db.models import LocalJobModel
from app.infrastructure.db.repositories.local_job_repo import LocalJobRepository
from app.infrastructure.local_tasks.definitions import TASK_DEFINITIONS
from app.logging import logger


class LocalTaskRunner:
    """Poll PostgreSQL and execute one durable job at a time."""

    def __init__(self) -> None:
        self._runner_task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        """Recover interrupted jobs and start the polling loop."""
        if self._runner_task is not None and not self._runner_task.done():
            return

        self._stopping = False
        async with async_session_factory() as session:
            recovered = await LocalJobRepository(session).recover_running()

        if recovered:
            logger.warning("local_jobs_recovered", count=recovered)

        self._runner_task = asyncio.create_task(self._worker_loop(), name="local-task-runner")
        logger.info("local_task_runner_started")

    async def stop(self) -> None:
        """Stop polling and let startup recovery reclaim an interrupted job."""
        self._stopping = True
        if self._runner_task is None:
            return

        self._runner_task.cancel()
        try:
            await self._runner_task
        except asyncio.CancelledError:
            pass
        finally:
            self._runner_task = None
        logger.info("local_task_runner_stopped")

    async def _worker_loop(self) -> None:
        while not self._stopping:
            try:
                job = await self._claim_next_job()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the poller alive across DB hiccups
                logger.error("local_job_claim_failed", error=str(exc))
                await asyncio.sleep(1)
                continue

            if job is None:
                await asyncio.sleep(1)
                continue

            try:
                await self._execute_job(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep processing later jobs
                logger.error(
                    "local_job_runner_iteration_failed",
                    job_id=str(job.id),
                    task_name=job.task_name,
                    error=str(exc),
                )

    async def _claim_next_job(self) -> LocalJobModel | None:
        async with async_session_factory() as session:
            return await LocalJobRepository(session).claim_next()

    async def _execute_job(self, job: LocalJobModel) -> None:
        definition = TASK_DEFINITIONS.get(job.task_name)
        if definition is None:
            await self._mark_failed(job.id, f"Unknown local task: {job.task_name}")
            return

        timeout_seconds = job.timeout_seconds
        if timeout_seconds is None:
            timeout_seconds = definition.timeout_seconds

        try:
            if timeout_seconds is None:
                await definition.handler(job.payload)
            else:
                async with asyncio.timeout(timeout_seconds):
                    await definition.handler(job.payload)
        except TimeoutError:
            message = f"Task exceeded its time limit ({timeout_seconds}s)."
            await self._handle_timeout(job, message)
        except Exception as exc:  # noqa: BLE001 - task boundary must capture failures
            await self._handle_error(job, exc)
        else:
            await self._mark_completed(job.id)

    async def _handle_error(self, job: LocalJobModel, exc: Exception) -> None:
        message = str(exc) or exc.__class__.__name__
        if job.task_name in {"execute_eval_run", "execute_session_eval_run"}:
            await self._fail_eval_run(job, message)

        if job.attempts <= job.max_retries:
            available_at = datetime.now(timezone.utc) + timedelta(seconds=job.retry_delay_seconds)
            await self._schedule_retry(job.id, available_at=available_at, error=message)
            logger.warning(
                "local_job_retry_scheduled",
                job_id=str(job.id),
                task_name=job.task_name,
                attempt=job.attempts,
                max_retries=job.max_retries,
                error=message,
            )
        else:
            await self._mark_failed(job.id, message)
            logger.error(
                "local_job_failed",
                job_id=str(job.id),
                task_name=job.task_name,
                attempts=job.attempts,
                error=message,
            )

    async def _handle_timeout(self, job: LocalJobModel, message: str) -> None:
        if job.task_name in {"execute_eval_run", "execute_session_eval_run"}:
            await self._fail_eval_run(job, message)
        await self._mark_failed(job.id, message)
        logger.error("local_job_timeout", job_id=str(job.id), task_name=job.task_name, error=message)

    async def _fail_eval_run(self, job: LocalJobModel, message: str) -> None:
        from app.infrastructure.queue.tasks import _fail_eval_run

        run_id = job.payload.get("run_id")
        if run_id is None:
            return
        await _fail_eval_run(str(run_id), message)

    async def _mark_completed(self, job_id: UUID) -> None:
        async with async_session_factory() as session:
            await LocalJobRepository(session).mark_completed(job_id)

    async def _schedule_retry(self, job_id: UUID, *, available_at: datetime, error: str) -> None:
        async with async_session_factory() as session:
            await LocalJobRepository(session).schedule_retry(
                job_id,
                available_at=available_at,
                error=error,
            )

    async def _mark_failed(self, job_id: UUID, error: str) -> None:
        async with async_session_factory() as session:
            await LocalJobRepository(session).mark_failed(job_id, error=error)


local_task_runner = LocalTaskRunner()
