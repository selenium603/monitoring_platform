"""Definitions and handlers for Trace/Eval local background jobs."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LocalTaskDefinition:
    """Runtime configuration for one local task type."""

    handler: Callable[[dict[str, Any]], Awaitable[Any]]
    max_retries: int
    retry_delay_seconds: int
    timeout_seconds: int | None
    retry_on_timeout: bool = False


async def process_trace_handler(payload: dict[str, Any]) -> Any:
    """Persist one trace using the existing trace worker implementation."""
    from app.infrastructure.queue.tasks import _persist_trace

    return await _persist_trace(payload["trace"])


async def execute_eval_handler(payload: dict[str, Any]) -> Any:
    """Execute a trace eval using the existing eval implementation."""
    from app.infrastructure.queue.tasks import _run_eval_run

    return await _run_eval_run(
        payload["run_id"],
        payload["project_id"],
        payload["trace_ids"],
        payload.get("trace_metric_map"),
    )


async def execute_session_eval_handler(payload: dict[str, Any]) -> Any:
    """Execute a session eval using the existing session eval implementation."""
    from app.infrastructure.queue.tasks import _run_session_eval

    return await _run_session_eval(
        payload["run_id"],
        payload["project_id"],
        payload["session_ids"],
    )


PROCESS_TRACE = LocalTaskDefinition(
    handler=process_trace_handler,
    max_retries=3,
    retry_delay_seconds=5,
    timeout_seconds=300,
)

EXECUTE_EVAL = LocalTaskDefinition(
    handler=execute_eval_handler,
    max_retries=2,
    retry_delay_seconds=10,
    timeout_seconds=3300,
)

EXECUTE_SESSION_EVAL = LocalTaskDefinition(
    handler=execute_session_eval_handler,
    max_retries=2,
    retry_delay_seconds=10,
    timeout_seconds=3300,
)

TASK_DEFINITIONS: dict[str, LocalTaskDefinition] = {
    "process_trace": PROCESS_TRACE,
    "execute_eval_run": EXECUTE_EVAL,
    "execute_session_eval_run": EXECUTE_SESSION_EVAL,
}
