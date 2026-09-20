"""In-process periodic scheduler for durable local jobs."""

import asyncio
from dataclasses import dataclass

from app.infrastructure.local_tasks.queue import enqueue_registered_job
from app.logging import logger


@dataclass(frozen=True)
class PeriodicTask:
    """Configuration for one recurring local task."""

    name: str
    task_name: str
    interval_seconds: float


PERIODIC_TASKS = (
    PeriodicTask(
        name="check-eval-monitors",
        task_name="check_eval_monitors",
        interval_seconds=300,
    ),
    PeriodicTask(
        name="dispatch-overage-billing",
        task_name="dispatch_overage_billing",
        interval_seconds=21600,
    ),
    PeriodicTask(
        name="dispatch-hobby-reset",
        task_name="dispatch_hobby_reset",
        interval_seconds=21600,
    ),
    PeriodicTask(
        name="expire-stale-invitations",
        task_name="expire_stale_invitations",
        interval_seconds=3600,
    ),
)


class LocalScheduler:
    """Enqueue periodic jobs from the FastAPI process."""

    def __init__(self, schedules: tuple[PeriodicTask, ...] = PERIODIC_TASKS) -> None:
        self._schedules = schedules
        self._tasks: list[asyncio.Task[None]] = []
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        """Start one interval loop per configured periodic task."""
        if self._tasks:
            return

        self._stop_event.clear()
        for schedule in self._schedules:
            task = asyncio.create_task(
                self._run_schedule(schedule),
                name=f"local-scheduler:{schedule.name}",
            )
            self._tasks.append(task)

        logger.info("local_scheduler_started", schedule_count=len(self._tasks))

    async def stop(self) -> None:
        """Stop all interval loops without creating new jobs."""
        if not self._tasks:
            return

        self._stop_event.set()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("local_scheduler_stopped")

    async def _run_schedule(self, schedule: PeriodicTask) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=schedule.interval_seconds,
                )
                break
            except TimeoutError:
                pass

            try:
                job_id = await enqueue_registered_job(
                    schedule.task_name,
                    {},
                    only_if_absent=True,
                )
                if job_id is None:
                    logger.debug(
                        "scheduled_job_skipped_active",
                        schedule=schedule.name,
                        task_name=schedule.task_name,
                    )
                    continue

                logger.info(
                    "scheduled_job_enqueued",
                    schedule=schedule.name,
                    task_name=schedule.task_name,
                    job_id=str(job_id),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one failed tick must not stop scheduling
                logger.error(
                    "local_scheduler_enqueue_failed",
                    schedule=schedule.name,
                    task_name=schedule.task_name,
                    error=str(exc),
                )


local_scheduler = LocalScheduler()
