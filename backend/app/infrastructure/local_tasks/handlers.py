"""Local durable task handlers for background processing.

Handlers run on the FastAPI event loop and open their own database session.

The durable local runner invokes these functions directly. Trace ingestion,
evaluation, billing, housekeeping, email, and CRM work all use the shared
PostgreSQL session factory.
"""

import asyncio
from typing import Any

from app.infrastructure.db.engine import async_session_factory
from app.logging import logger


# ---------------------------------------------------------------------------
# Trace persistence
# ---------------------------------------------------------------------------


async def persist_trace(payload: dict[str, Any]) -> dict[str, str]:
    """Async helper that opens a DB session and saves the trace."""
    from app.core.traces.entities import Trace
    from app.infrastructure.db.repositories.trace_repo import TraceRepository

    trace = Trace.model_validate(payload)

    async with async_session_factory() as session:
        repo = TraceRepository(session)
        await repo.upsert_trace(trace)
        await session.commit()

    logger.info("trace_persisted", trace_id=str(trace.trace_id))
    return {"trace_id": str(trace.trace_id), "status": "persisted"}


# ---------------------------------------------------------------------------
# Eval run execution
# ---------------------------------------------------------------------------


async def run_eval_run(
    run_id: str,
    project_id: str,
    trace_ids: list[str],
    trace_metric_map: dict[str, list[str]] | None = None,
) -> dict[str, str]:
    """Run requested metrics against a batch of traces.

    The eval run row must already exist in the database (created by
    the API handler).  This task transitions it through
    PENDING -> RUNNING -> COMPLETED/FAILED.

    Args:
        run_id: UUID of the eval run.
        project_id: UUID of the owning project.
        trace_ids: List of trace UUID strings to evaluate.
        trace_metric_map: Optional per-trace metric override. When set,
            each trace only runs the metrics listed for it instead of
            all metrics from the run. Used by retry to avoid Cartesian
            product re-evaluation.

    Returns:
        A dict summarising the eval run outcome.
    """
    from datetime import datetime, timezone
    from uuid import UUID, uuid4

    from app.core.evals.entities import TraceScore
    from app.core.evals.metrics import get_metric
    from app.core.evals.metrics.base import MetricResult
    from app.infrastructure.db.repositories.eval_repo import EvalRepository
    from app.infrastructure.db.repositories.trace_repo import TraceRepository
    from app.infrastructure.llm.engine import LLMEngine
    from app.registry.constants import EvaluationStatus, ScoreDataType, ScoreSource, ScoreStatus

    run_uuid = UUID(run_id)
    proj_uuid = UUID(project_id)

    llm = LLMEngine()

    async with async_session_factory() as session:
        eval_repo = EvalRepository(session)
        trace_repo = TraceRepository(session)

        await eval_repo.delete_scores_for_run(run_uuid, proj_uuid)
        await eval_repo.reset_run_counters(run_uuid)
        await eval_repo.update_run_status(run_uuid, EvaluationStatus.RUNNING)
        await session.commit()

        run = await eval_repo.get_eval_run(run_uuid, proj_uuid)
        if run is None:
            raise ValueError(f"Eval run {run_id} not found")

        for tid_str in trace_ids:
            tid = UUID(tid_str)
            trace = await trace_repo.get_trace(tid, proj_uuid)
            if trace is None:
                logger.warning("eval_run_trace_not_found", run_id=run_id, trace_id=tid_str)
                await eval_repo.increment_progress(run_uuid)
                await session.commit()
                continue

            metrics_for_trace = (
                trace_metric_map.get(tid_str, run.metric_names) if trace_metric_map else run.metric_names
            )
            for metric_name in metrics_for_trace:
                metric_cls = get_metric(metric_name)
                metric = metric_cls()

                now = datetime.now(timezone.utc)
                try:
                    result: MetricResult = await metric.evaluate(trace, llm, model=run.model)

                    score = TraceScore(
                        id=uuid4(),
                        trace_id=tid,
                        project_id=proj_uuid,
                        name=metric_name,
                        data_type=ScoreDataType.NUMERIC,
                        value=str(round(result.score, 4)),
                        source=ScoreSource.AUTOMATED,
                        status=ScoreStatus.SUCCESS,
                        eval_run_id=run_uuid,
                        reason=result.reason,
                        environment=trace.environment,
                        metadata=result.metadata,
                        created_at=now,
                        updated_at=now,
                    )
                    await eval_repo.create_score(score)

                    logger.info(
                        "metric_completed",
                        run_id=run_id,
                        trace_id=tid_str,
                        metric=metric_name,
                        score=result.score,
                    )
                except Exception as exc:
                    logger.error(
                        "metric_failed",
                        run_id=run_id,
                        trace_id=tid_str,
                        metric=metric_name,
                        error=str(exc),
                    )
                    failed_score = TraceScore(
                        id=uuid4(),
                        trace_id=tid,
                        project_id=proj_uuid,
                        name=metric_name,
                        data_type=ScoreDataType.NUMERIC,
                        value=None,
                        source=ScoreSource.AUTOMATED,
                        status=ScoreStatus.FAILED,
                        eval_run_id=run_uuid,
                        reason=f"Metric execution failed: {exc}",
                        environment=trace.environment,
                        metadata={},
                        created_at=now,
                        updated_at=now,
                    )
                    await eval_repo.create_score(failed_score)
                    await eval_repo.increment_failed(run_uuid)

            await eval_repo.increment_progress(run_uuid)
            await session.commit()

        await eval_repo.update_run_status(run_uuid, EvaluationStatus.COMPLETED)
        await session.commit()

    logger.info("eval_run_completed", run_id=run_id)
    return {"run_id": run_id, "status": "completed"}


async def fail_eval_run(run_id: str, error_message: str) -> None:
    """Mark an eval run as FAILED on unrecoverable errors."""
    from uuid import UUID

    from app.infrastructure.db.repositories.eval_repo import EvalRepository
    from app.registry.constants import EvaluationStatus

    try:
        async with async_session_factory() as session:
            repo = EvalRepository(session)
            await repo.update_run_status(UUID(run_id), EvaluationStatus.FAILED, error_message=error_message)
            await session.commit()
    except Exception:
        logger.error("fail_eval_run_update_failed", run_id=run_id)


# ---------------------------------------------------------------------------
# Eval monitor tick
# ---------------------------------------------------------------------------


async def check_eval_monitors() -> dict[str, Any]:
    """Query due monitors, reschedule them, and enqueue local jobs."""
    from datetime import datetime, timezone

    from app.core.evals.cadence import compute_next_run
    from app.infrastructure.db.repositories.eval_repo import EvalRepository

    from app.infrastructure.local_tasks.queue import enqueue_registered_job

    now = datetime.now(timezone.utc)
    monitors_to_dispatch: list[tuple[str, str]] = []

    async with async_session_factory() as session:
        eval_repo = EvalRepository(session)
        due_monitors = await eval_repo.get_due_monitors(now)

        for monitor in due_monitors:
            next_run = compute_next_run(monitor.cadence, now)
            await eval_repo.reschedule_monitor(
                monitor.id,
                next_run_at=next_run,
            )
            monitors_to_dispatch.append((str(monitor.id), str(monitor.project_id)))

        await session.commit()

    for monitor_id, project_id in monitors_to_dispatch:
        await enqueue_registered_job(
            "process_single_monitor",
            {
                "monitor_id": monitor_id,
                "project_id": project_id,
            },
        )

    dispatched = len(monitors_to_dispatch)
    summary = {"status": "completed", "dispatched": dispatched}
    logger.info("check_eval_monitors_done", **summary)
    return summary


async def process_single_monitor(monitor_id: str, project_id: str) -> dict[str, str]:
    """Process a single due monitor: check for new data, spawn an eval run if needed.

    Runs as an independent durable local job.
    """
    from datetime import datetime, timezone
    from uuid import UUID

    from app.infrastructure.db.models import ProjectModel
    from app.infrastructure.db.repositories.eval_repo import EvalRepository
    from app.registry.constants import UsageCategory
    from app.services.eval_service import EvalService
    from app.services.usage_service import UsageService

    mid = UUID(monitor_id)
    pid = UUID(project_id)

    async with async_session_factory() as session:
        eval_repo = EvalRepository(session)
        svc = EvalService(session)

        monitor = await eval_repo.get_monitor(mid, pid)
        if monitor is None:
            logger.warning("process_single_monitor_not_found", monitor_id=monitor_id)
            return {"monitor_id": monitor_id, "status": "not_found"}

        if await svc.should_skip_monitor(monitor):
            logger.info("monitor_skipped_no_changes", monitor_id=monitor_id)
            return {"monitor_id": monitor_id, "status": "skipped"}

        project = await session.get(ProjectModel, pid)
        if project is None:
            logger.error("process_single_monitor_project_missing", monitor_id=monitor_id, project_id=project_id)
            return {"monitor_id": monitor_id, "status": "project_missing"}

        run, target_ids = await svc._spawn_run_for_monitor(monitor)

        category = UsageCategory.TRACE_EVALS if monitor.target_type == "TRACE" else UsageCategory.SESSION_EVALS
        billable_units = run.total_targets * len(run.metric_names)
        usage_svc = UsageService(session)
        await usage_svc.check_and_increment(project.org_id, category, count=billable_units)
        try:
            now = datetime.now(timezone.utc)
            await eval_repo.advance_monitor(
                mid,
                last_run_at=now,
                last_run_id=run.id,
            )
            await session.commit()
        except Exception:
            await usage_svc.rollback_increment(project.org_id, category, count=billable_units)
            raise

        await svc._dispatch_monitor_run(monitor.target_type, run.id, monitor.project_id, target_ids)

        logger.info(
            "monitor_run_spawned",
            monitor_id=monitor_id,
            run_id=str(run.id),
            billable_units=billable_units,
        )
        return {"monitor_id": monitor_id, "status": "spawned", "run_id": str(run.id)}


# ---------------------------------------------------------------------------
# Session eval run execution
# ---------------------------------------------------------------------------

SIGNAL_METRICS = ["confidence", "coherence", "loop_detection", "tool_correctness"]


async def run_session_eval(
    run_id: str,
    project_id: str,
    session_ids: list[str],
) -> dict[str, str]:
    """Run session-level metrics across one or more sessions.

    Fully self-contained: resolves traces per session, computes all
    trace-level signals (persisting each as a TraceScore), then passes
    the precomputed signals to session metrics for pure aggregation.
    """
    """Core async logic for executing a session eval run."""
    from datetime import datetime, timezone
    from uuid import UUID, uuid4

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.core.evals.entities import SessionScore, TraceScore
    from app.core.evals.metrics import get_metric, get_session_metric
    from app.core.evals.metrics.base import MetricResult
    from app.core.evals.metrics.utils import to_text
    from app.core.traces.entities import Span, Trace
    from app.infrastructure.db.models import SpanModel, TraceModel
    from app.infrastructure.db.repositories.eval_repo import EvalRepository
    from app.infrastructure.llm.engine import LLMEngine
    from app.registry.constants import (
        EvaluationStatus,
        ScoreDataType,
        ScoreSource,
        ScoreStatus,
        SpanKind,
        SpanStatusCode,
        TraceStatus,
    )

    run_uuid = UUID(run_id)
    proj_uuid = UUID(project_id)

    llm = LLMEngine()

    async with async_session_factory() as session:
        eval_repo = EvalRepository(session)

        # Phase 0 -- Setup
        await eval_repo.delete_session_scores_for_run(run_uuid, proj_uuid)
        await eval_repo.delete_scores_for_run(run_uuid, proj_uuid)
        await eval_repo.reset_run_counters(run_uuid)
        await eval_repo.update_run_status(run_uuid, EvaluationStatus.RUNNING)
        await session.commit()

        run = await eval_repo.get_eval_run(run_uuid, proj_uuid)
        if run is None:
            raise ValueError(f"Eval run {run_id} not found")

        signal_weights = run.filters.get("signal_weights")

        # Phase 1 -- Per-session processing
        for sid in session_ids:
            # Step A -- Resolve traces
            stmt = (
                select(TraceModel)
                .options(selectinload(TraceModel.spans))
                .where(TraceModel.project_id == proj_uuid, TraceModel.session_id == sid)
                .order_by(TraceModel.started_at.asc())
            )
            result = await session.execute(stmt)
            trace_rows = result.scalars().all()

            if not trace_rows:
                logger.warning("session_eval_no_traces", run_id=run_id, session_id=sid)
                await eval_repo.increment_progress(run_uuid)
                await session.commit()
                continue

            traces: list[Trace] = []
            for row in trace_rows:
                spans = [
                    Span(
                        span_id=s.span_id,
                        trace_id=s.trace_id,
                        parent_span_id=s.parent_span_id,
                        name=s.name,
                        kind=SpanKind(s.kind),
                        status=SpanStatusCode(s.status),
                        input=s.input,
                        output=s.output,
                        model=s.model,
                        token_usage=s.token_usage,
                        metadata=s.metadata_,
                        started_at=s.started_at,
                        ended_at=s.ended_at,
                        error=s.error,
                        completion_start_time=s.completion_start_time,
                        model_parameters=s.model_parameters,
                        cost=s.cost,
                    )
                    for s in row.spans
                ]
                traces.append(
                    Trace(
                        trace_id=row.trace_id,
                        project_id=row.project_id,
                        name=row.name,
                        status=TraceStatus(row.status),
                        input=row.input,
                        output=row.output,
                        metadata=row.metadata_,
                        started_at=row.started_at,
                        ended_at=row.ended_at,
                        session_id=row.session_id,
                        user_id=row.user_id,
                        tags=row.tags or [],
                        environment=row.environment,
                        release=row.release,
                        spans=spans,
                    )
                )

            # Step B -- Warm embedding cache
            all_texts = []
            for t in traces:
                inp = to_text(t.input)
                out = to_text(t.output)
                if inp:
                    all_texts.append(inp)
                if out:
                    all_texts.append(out)
            if all_texts:
                try:
                    await llm.embed_texts(all_texts)
                except Exception as exc:
                    logger.warning("session_eval_embed_warmup_failed", error=str(exc))

            # Step C -- Compute and persist trace-level signals
            precomputed_signals: dict[str, dict[str, float]] = {}

            for i, trace_entity in enumerate(traces):
                tid = trace_entity.trace_id
                trace_signals: dict[str, float] = {}

                for signal_name in SIGNAL_METRICS:
                    existing = await eval_repo.find_existing_trace_score(tid, signal_name)

                    if existing and existing.value is not None:
                        score_value = float(existing.value)
                        logger.debug(
                            "signal_reused",
                            run_id=run_id,
                            trace_id=str(tid),
                            signal=signal_name,
                            score=score_value,
                        )
                    else:
                        try:
                            metric_cls = get_metric(signal_name)
                            metric_instance = metric_cls()
                            result: MetricResult = await metric_instance.evaluate(
                                trace_entity,
                                llm,
                                model=run.model,
                                session_traces=traces[:i],
                            )
                            score_value = result.score

                            now = datetime.now(timezone.utc)
                            trace_score = TraceScore(
                                id=uuid4(),
                                trace_id=tid,
                                project_id=proj_uuid,
                                name=signal_name,
                                data_type=ScoreDataType.NUMERIC,
                                value=str(round(score_value, 4)),
                                source=ScoreSource.AUTOMATED,
                                status=ScoreStatus.SUCCESS,
                                eval_run_id=run_uuid,
                                reason=result.reason,
                                environment=trace_entity.environment,
                                metadata=result.metadata or {},
                                created_at=now,
                                updated_at=now,
                            )
                            await eval_repo.create_score(trace_score)

                            logger.info(
                                "signal_computed",
                                run_id=run_id,
                                trace_id=str(tid),
                                signal=signal_name,
                                score=score_value,
                            )
                        except Exception as exc:
                            logger.error(
                                "signal_failed",
                                run_id=run_id,
                                trace_id=str(tid),
                                signal=signal_name,
                                error=str(exc),
                            )
                            score_value = None

                            now = datetime.now(timezone.utc)
                            failed_score = TraceScore(
                                id=uuid4(),
                                trace_id=tid,
                                project_id=proj_uuid,
                                name=signal_name,
                                data_type=ScoreDataType.NUMERIC,
                                value=None,
                                source=ScoreSource.AUTOMATED,
                                status=ScoreStatus.FAILED,
                                eval_run_id=run_uuid,
                                reason=f"Signal computation failed: {exc}",
                                environment=trace_entity.environment,
                                metadata={},
                                created_at=now,
                                updated_at=now,
                            )
                            await eval_repo.create_score(failed_score)

                    if score_value is not None:
                        trace_signals[signal_name] = score_value

                precomputed_signals[str(tid)] = trace_signals

            await session.commit()

            # Step D -- Run session metrics (pure aggregation)
            for metric_name in run.metric_names:
                now = datetime.now(timezone.utc)
                try:
                    session_metric_cls = get_session_metric(metric_name)
                    session_metric = session_metric_cls()

                    result = await session_metric.evaluate(
                        session_id=sid,
                        traces=traces,
                        llm=llm,
                        model=run.model,
                        signal_weights=signal_weights,
                        precomputed_signals=precomputed_signals,
                    )

                    score_entity = SessionScore(
                        id=uuid4(),
                        session_id=sid,
                        project_id=proj_uuid,
                        name=metric_name,
                        data_type=ScoreDataType.NUMERIC,
                        value=str(round(result.score, 4)),
                        source=ScoreSource.AUTOMATED,
                        status=ScoreStatus.SUCCESS,
                        eval_run_id=run_uuid,
                        reason=result.reason,
                        metadata=result.metadata or {},
                        created_at=now,
                        updated_at=now,
                    )
                    await eval_repo.create_session_score(score_entity)

                    logger.info(
                        "session_metric_completed",
                        run_id=run_id,
                        session_id=sid,
                        metric=metric_name,
                        score=result.score,
                    )
                except Exception as exc:
                    logger.error(
                        "session_metric_failed",
                        run_id=run_id,
                        session_id=sid,
                        metric=metric_name,
                        error=str(exc),
                    )
                    failed_entity = SessionScore(
                        id=uuid4(),
                        session_id=sid,
                        project_id=proj_uuid,
                        name=metric_name,
                        data_type=ScoreDataType.NUMERIC,
                        value=None,
                        source=ScoreSource.AUTOMATED,
                        status=ScoreStatus.FAILED,
                        eval_run_id=run_uuid,
                        reason=f"Session metric failed: {exc}",
                        metadata={},
                        created_at=now,
                        updated_at=now,
                    )
                    await eval_repo.create_session_score(failed_entity)
                    await eval_repo.increment_failed(run_uuid)

            # Step E -- Progress
            await eval_repo.increment_progress(run_uuid)
            await session.commit()

        # Phase 2 -- Finalize
        await eval_repo.update_run_status(run_uuid, EvaluationStatus.COMPLETED)
        await session.commit()

    logger.info("session_eval_run_completed", run_id=run_id)
    return {"run_id": run_id, "status": "completed"}


# ---------------------------------------------------------------------------
# Billing tasks (dispatcher + per-organization job pattern)
#
# Each periodic task is a lightweight dispatcher that queries eligible org
# IDs, then fans out one durable local job per organization.
# ---------------------------------------------------------------------------


# -- Overage billing ----------------------------------------------------------


async def dispatch_overage_billing() -> dict[str, Any]:
    """Dispatcher: query paid org IDs and fan out billing tasks."""
    from app.infrastructure.db.repositories.billing_repo import BillingRepository
    from app.infrastructure.local_tasks.queue import enqueue_registered_job

    async with async_session_factory() as session:
        org_ids = await BillingRepository(session).list_paid_active_org_ids()

    for oid in org_ids:
        await enqueue_registered_job(
            "bill_single_org",
            {"org_id": str(oid)},
        )

    logger.info("dispatch_overage_billing_done", dispatched=len(org_ids))
    return {"status": "dispatched", "count": len(org_ids)}


async def bill_single_org(org_id_str: str) -> dict[str, str]:
    """Lock the subscription, compute the DB delta, and report to Stripe.

    The local runner limits retries and preserves one-job-at-a-time execution.
    """
    from uuid import UUID

    from app.services.billing_service import BillingService

    org_id = UUID(org_id_str)
    async with async_session_factory() as session:
        billing_svc = BillingService(session)
        if not await billing_svc.try_acquire_overage_lock(org_id):
            logger.info("bill_single_org_skipped_locked", org_id=org_id_str)
            return {"org_id": org_id_str, "status": "skipped_locked"}

        try:
            reported = await billing_svc.report_overages_to_stripe(org_id)
            await session.commit()
        except Exception:
            await session.rollback()
            raise

    return {"org_id": org_id_str, "status": "reported" if reported else "no_overage"}


# -- HOBBY period reset -------------------------------------------------------


async def dispatch_hobby_reset() -> dict[str, Any]:
    """Dispatcher: query HOBBY org IDs due for reset and fan out tasks."""
    from datetime import datetime, timezone

    from app.infrastructure.db.repositories.billing_repo import BillingRepository
    from app.infrastructure.local_tasks.queue import enqueue_registered_job

    now = datetime.now(timezone.utc)
    async with async_session_factory() as session:
        org_ids = await BillingRepository(session).list_hobby_org_ids_due_for_reset(now)

    for oid in org_ids:
        await enqueue_registered_job(
            "reset_single_hobby_org",
            {"org_id": str(oid)},
        )

    logger.info("dispatch_hobby_reset_done", dispatched=len(org_ids))
    return {"status": "dispatched", "count": len(org_ids)}


async def reset_single_hobby_org(org_id_str: str) -> dict[str, str]:
    """Advance the billing period for one HOBBY organization."""
    from datetime import timedelta
    from uuid import UUID

    from app.infrastructure.db.repositories.billing_repo import BillingRepository

    org_id = UUID(org_id_str)
    async with async_session_factory() as session:
        billing_repo = BillingRepository(session)
        sub = await billing_repo.get_subscription_by_org(org_id)
        if sub is None:
            return {"org_id": org_id_str, "status": "no_subscription"}

        new_start = sub.current_period_end
        new_end = new_start + timedelta(days=30)

        await billing_repo.advance_period(org_id, new_start, new_end)
        await billing_repo.create_usage_record(org_id, new_start, new_end)
        await session.commit()


    return {"org_id": org_id_str, "status": "reset"}


# ---------------------------------------------------------------------------
# Email – welcome sequence
# ---------------------------------------------------------------------------

async def send_welcome_email(email: str) -> dict[str, str]:
    """Send a welcome email without blocking the FastAPI event loop."""
    from app.services.email_service import EmailService

    svc = EmailService()
    if not await asyncio.to_thread(svc.is_configured):
        return {"status": "skipped", "reason": "resend_not_configured"}

    await asyncio.to_thread(svc.send_welcome_email, to=email)
    return {"status": "sent", "email": email}


async def send_followup_email(email: str) -> dict[str, str]:
    """Send a follow-up email without blocking the FastAPI event loop."""
    from app.services.email_service import EmailService

    svc = EmailService()
    if not await asyncio.to_thread(svc.is_configured):
        return {"status": "skipped", "reason": "resend_not_configured"}

    await asyncio.to_thread(svc.send_followup_email, to=email)
    return {"status": "sent", "email": email}


async def send_invitation_email(
    to: str,
    org_name: str,
    inviter_name: str,
    role: str,
    app_url: str,
) -> dict[str, str]:
    """Send an invitation notification email without blocking the event loop."""
    from app.services.email_service import EmailService

    svc = EmailService()
    if not await asyncio.to_thread(svc.is_configured):
        return {"status": "skipped", "reason": "resend_not_configured"}

    await asyncio.to_thread(
        svc.send_invitation_email,
        to=to,
        org_name=org_name,
        inviter_name=inviter_name,
        role=role,
        app_url=app_url,
    )
    return {"status": "sent", "email": to}


# ---------------------------------------------------------------------------
# Invitation housekeeping
# ---------------------------------------------------------------------------


async def expire_stale_invitations() -> dict[str, Any]:
    from app.infrastructure.db.repositories.invitation_repo import InvitationRepository

    async with async_session_factory() as session:
        count = await InvitationRepository(session).expire_stale_invitations()
        await session.commit()

    logger.info("expire_stale_invitations_done", expired=count)
    return {"status": "done", "expired": count}


# ---------------------------------------------------------------------------
# CRM – Attio contact sync
# ---------------------------------------------------------------------------


async def sync_new_user_to_crm(email: str) -> dict[str, str]:
    """Sync a new user to Attio without blocking the FastAPI event loop."""
    from app.services.crm_service import CrmService

    svc = CrmService()
    if not await asyncio.to_thread(svc.is_configured):
        logger.debug("attio_skip_unconfigured", email=email)
        return {"status": "skipped", "reason": "attio_not_configured"}

    await asyncio.to_thread(svc.sync_contact, email=email)
    return {"status": "synced", "email": email}
