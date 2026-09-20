"""Definitions and handlers for local background jobs."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.logging import logger


@dataclass(frozen=True)
class LocalTaskDefinition:
    """Runtime configuration for one local task type."""

    handler: Callable[[dict[str, Any]], Awaitable[Any]]
    max_retries: int
    retry_delay_seconds: int
    timeout_seconds: int | None
    retry_on_timeout: bool = False
    retry_backoff: bool = False
    retry_backoff_max_seconds: int | None = None
    min_interval_seconds: float | None = None


async def process_trace_handler(payload: dict[str, Any]) -> Any:
    """Persist one trace using the local task implementation."""
    from app.infrastructure.local_tasks.handlers import persist_trace

    return await persist_trace(payload["trace"])


async def execute_eval_handler(payload: dict[str, Any]) -> Any:
    """Execute a trace eval using the existing eval implementation."""
    from app.infrastructure.local_tasks.handlers import run_eval_run

    return await run_eval_run(
        payload["run_id"],
        payload["project_id"],
        payload["trace_ids"],
        payload.get("trace_metric_map"),
    )


async def execute_session_eval_handler(payload: dict[str, Any]) -> Any:
    """Execute a session eval using the existing session eval implementation."""
    from app.infrastructure.local_tasks.handlers import run_session_eval

    return await run_session_eval(
        payload["run_id"],
        payload["project_id"],
        payload["session_ids"],
    )


async def check_eval_monitors_handler(payload: dict[str, Any]) -> Any:
    """Check due monitors and enqueue one local job per monitor."""
    from app.infrastructure.local_tasks.handlers import check_eval_monitors

    try:
        return await check_eval_monitors()
    except Exception as exc:  # noqa: BLE001 - keep the dispatcher job terminal
        logger.error("check_eval_monitors_failed", error=str(exc))
        return {"error": str(exc)}


async def process_single_monitor_handler(payload: dict[str, Any]) -> Any:
    """Process one due monitor and preserve quota-exhaustion semantics."""
    from app.infrastructure.local_tasks.handlers import process_single_monitor
    from app.registry.exceptions import QuotaExceededError

    try:
        return await process_single_monitor(payload["monitor_id"], payload["project_id"])
    except QuotaExceededError:
        logger.warning(
            "process_single_monitor_quota_exceeded",
            monitor_id=payload["monitor_id"],
        )
        return {
            "monitor_id": payload["monitor_id"],
            "status": "quota_exceeded",
        }


async def dispatch_overage_billing_handler(payload: dict[str, Any]) -> Any:
    """Dispatch overage billing jobs for paid organizations."""
    from app.infrastructure.local_tasks.handlers import dispatch_overage_billing

    try:
        return await dispatch_overage_billing()
    except Exception as exc:  # noqa: BLE001 - keep the dispatcher job terminal
        logger.error("dispatch_overage_billing_failed", error=str(exc))
        return {"error": str(exc)}


async def bill_single_org_handler(payload: dict[str, Any]) -> Any:
    """Bill one organization while treating missing Stripe configuration as terminal."""
    from app.infrastructure.local_tasks.handlers import bill_single_org
    from app.services.billing_service import StripeNotConfiguredError

    try:
        return await bill_single_org(payload["org_id"])
    except StripeNotConfiguredError as exc:
        logger.error(
            "bill_single_org_misconfigured",
            org_id=payload["org_id"],
            error=str(exc),
        )
        return {
            "org_id": payload["org_id"],
            "status": "stripe_not_configured",
        }


async def dispatch_hobby_reset_handler(payload: dict[str, Any]) -> Any:
    """Dispatch period-reset jobs for due hobby organizations."""
    from app.infrastructure.local_tasks.handlers import dispatch_hobby_reset

    try:
        return await dispatch_hobby_reset()
    except Exception as exc:  # noqa: BLE001 - keep the dispatcher job terminal
        logger.error("dispatch_hobby_reset_failed", error=str(exc))
        return {"error": str(exc)}


async def reset_single_hobby_org_handler(payload: dict[str, Any]) -> Any:
    """Reset one hobby organization's billing period and usage counters."""
    from app.infrastructure.local_tasks.handlers import reset_single_hobby_org

    return await reset_single_hobby_org(payload["org_id"])


async def expire_stale_invitations_handler(payload: dict[str, Any]) -> Any:
    """Expire pending invitations whose deadline has passed."""
    from app.infrastructure.local_tasks.handlers import expire_stale_invitations

    try:
        return await expire_stale_invitations()
    except Exception as exc:  # noqa: BLE001 - keep the housekeeping job terminal
        logger.error("expire_stale_invitations_failed", error=str(exc))
        return {"error": str(exc)}


async def send_welcome_email_handler(payload: dict[str, Any]) -> Any:
    from app.infrastructure.local_tasks.handlers import send_welcome_email

    return await send_welcome_email(payload["email"])


async def send_followup_email_handler(payload: dict[str, Any]) -> Any:
    from app.infrastructure.local_tasks.handlers import send_followup_email

    return await send_followup_email(payload["email"])


async def send_invitation_email_handler(payload: dict[str, Any]) -> Any:
    from app.infrastructure.local_tasks.handlers import send_invitation_email

    return await send_invitation_email(
        to=payload["to"],
        org_name=payload["org_name"],
        inviter_name=payload["inviter_name"],
        role=payload["role"],
        app_url=payload["app_url"],
    )


async def sync_new_user_to_crm_handler(payload: dict[str, Any]) -> Any:
    from app.infrastructure.local_tasks.handlers import sync_new_user_to_crm

    return await sync_new_user_to_crm(payload["email"])


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

CHECK_EVAL_MONITORS = LocalTaskDefinition(
    handler=check_eval_monitors_handler,
    max_retries=0,
    retry_delay_seconds=0,
    timeout_seconds=300,
)

PROCESS_SINGLE_MONITOR = LocalTaskDefinition(
    handler=process_single_monitor_handler,
    max_retries=2,
    retry_delay_seconds=30,
    timeout_seconds=300,
)

DISPATCH_OVERAGE_BILLING = LocalTaskDefinition(
    handler=dispatch_overage_billing_handler,
    max_retries=0,
    retry_delay_seconds=0,
    timeout_seconds=300,
)

BILL_SINGLE_ORG = LocalTaskDefinition(
    handler=bill_single_org_handler,
    max_retries=2,
    retry_delay_seconds=15,
    timeout_seconds=300,
    retry_on_timeout=True,
)

DISPATCH_HOBBY_RESET = LocalTaskDefinition(
    handler=dispatch_hobby_reset_handler,
    max_retries=0,
    retry_delay_seconds=0,
    timeout_seconds=300,
)

RESET_SINGLE_HOBBY_ORG = LocalTaskDefinition(
    handler=reset_single_hobby_org_handler,
    max_retries=2,
    retry_delay_seconds=10,
    timeout_seconds=300,
    retry_on_timeout=True,
)

EXPIRE_STALE_INVITATIONS = LocalTaskDefinition(
    handler=expire_stale_invitations_handler,
    max_retries=0,
    retry_delay_seconds=0,
    timeout_seconds=300,
)

SEND_WELCOME_EMAIL = LocalTaskDefinition(
    handler=send_welcome_email_handler,
    max_retries=3,
    retry_delay_seconds=1,
    timeout_seconds=300,
    retry_on_timeout=True,
    retry_backoff=True,
    retry_backoff_max_seconds=600,
    min_interval_seconds=0.5,
)

SEND_FOLLOWUP_EMAIL = LocalTaskDefinition(
    handler=send_followup_email_handler,
    max_retries=3,
    retry_delay_seconds=1,
    timeout_seconds=300,
    retry_on_timeout=True,
    retry_backoff=True,
    retry_backoff_max_seconds=600,
    min_interval_seconds=0.5,
)

SEND_INVITATION_EMAIL = LocalTaskDefinition(
    handler=send_invitation_email_handler,
    max_retries=3,
    retry_delay_seconds=1,
    timeout_seconds=300,
    retry_on_timeout=True,
    retry_backoff=True,
    retry_backoff_max_seconds=600,
    min_interval_seconds=0.5,
)

SYNC_NEW_USER_TO_CRM = LocalTaskDefinition(
    handler=sync_new_user_to_crm_handler,
    max_retries=3,
    retry_delay_seconds=1,
    timeout_seconds=300,
    retry_on_timeout=True,
    retry_backoff=True,
    retry_backoff_max_seconds=600,
    min_interval_seconds=0.5,
)

TASK_DEFINITIONS: dict[str, LocalTaskDefinition] = {
    "process_trace": PROCESS_TRACE,
    "execute_eval_run": EXECUTE_EVAL,
    "execute_session_eval_run": EXECUTE_SESSION_EVAL,
    "check_eval_monitors": CHECK_EVAL_MONITORS,
    "process_single_monitor": PROCESS_SINGLE_MONITOR,
    "dispatch_overage_billing": DISPATCH_OVERAGE_BILLING,
    "bill_single_org": BILL_SINGLE_ORG,
    "dispatch_hobby_reset": DISPATCH_HOBBY_RESET,
    "reset_single_hobby_org": RESET_SINGLE_HOBBY_ORG,
    "expire_stale_invitations": EXPIRE_STALE_INVITATIONS,
    "send_welcome_email": SEND_WELCOME_EMAIL,
    "send_followup_email": SEND_FOLLOWUP_EMAIL,
    "send_invitation_email": SEND_INVITATION_EMAIL,
    "sync_new_user_to_crm": SYNC_NEW_USER_TO_CRM,
}
