"""Durable in-process background task execution."""

from app.infrastructure.local_tasks.queue import enqueue_local_job

__all__ = ["enqueue_local_job"]
