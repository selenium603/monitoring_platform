"""PostgreSQL-backed usage tracking and quota enforcement.

Usage records are the source of truth. Quota checks and increments are
performed in PostgreSQL so API requests, local jobs, and billing workers all
observe the same counters without a cache synchronization window.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.billing.entities import UsageSummary
from app.core.billing.plans import get_limit_for_category, get_plan_config
from app.infrastructure.db.repositories.billing_repo import BillingRepository
from app.logging import logger
from app.registry.constants import SubscriptionPlan, SubscriptionStatus, UsageCategory
from app.registry.exceptions import QuotaExceededError


class UsageService:
    """Track billable actions directly in PostgreSQL."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = BillingRepository(session)

    async def check_and_increment(
        self,
        org_id: UUID,
        category: UsageCategory | str,
        count: int = 1,
    ) -> int:
        """Atomically increment usage and enforce the subscription quota."""
        category = UsageCategory(category)
        if count <= 0:
            raise ValueError("Usage increment must be positive.")

        sub = await self._repo.get_subscription_by_org_for_update(org_id)
        if sub is None:
            raise QuotaExceededError("No active subscription found for this organization.")
        if sub.status not in (SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE):
            raise QuotaExceededError("Your subscription is not active.")

        plan = SubscriptionPlan(sub.plan)
        plan_config = get_plan_config(plan)
        limit = get_limit_for_category(plan, category)
        hard_limit = -1 if plan_config.pay_as_you_go or limit is None else limit

        await self._repo.get_or_create_usage_record(
            org_id,
            sub.current_period_start,
            sub.current_period_end,
        )
        new_value = await self._repo.increment_usage(
            org_id,
            sub.current_period_start,
            category,
            count,
            hard_limit,
        )
        if new_value is None:
            raise QuotaExceededError(
                f"You've reached the {category.value} limit ({limit}) for your {sub.plan} plan. "
                "Please upgrade to continue."
            )
        return new_value

    async def rollback_increment(
        self,
        org_id: UUID,
        category: UsageCategory | str,
        count: int = 1,
    ) -> None:
        """Best-effort compensating decrement after a failed operation."""
        try:
            category = UsageCategory(category)
            if count <= 0:
                return
            sub = await self._repo.get_subscription_by_org_for_update(org_id)
            if sub is None:
                return
            await self._repo.decrement_usage(
                org_id,
                sub.current_period_start,
                category,
                count,
            )
        except Exception:
            logger.warning(
                "rollback_increment_failed",
                org_id=str(org_id),
                category=str(category),
                count=count,
            )

    async def get_current_usage(self, org_id: UUID) -> UsageSummary:
        """Return a snapshot of the current period's usage from PostgreSQL."""
        sub = await self._repo.get_subscription_by_org(org_id)
        if sub is None:
            now = datetime.now(timezone.utc)
            return UsageSummary(
                plan=SubscriptionPlan.HOBBY,
                status=SubscriptionStatus.ACTIVE,
                period_start=now,
                period_end=now,
            )

        plan = SubscriptionPlan(sub.plan)
        plan_config = get_plan_config(plan)
        usage = await self._repo.get_current_usage_record(org_id, sub.current_period_start)
        return UsageSummary(
            plan=plan,
            status=SubscriptionStatus(sub.status),
            period_start=sub.current_period_start,
            period_end=sub.current_period_end,
            traces=usage.trace_count if usage else 0,
            trace_evals=usage.trace_eval_count if usage else 0,
            session_evals=usage.session_eval_count if usage else 0,
            limits={
                "base_traces": plan_config.base_traces,
                "base_trace_evals": plan_config.base_trace_evals,
                "base_session_evals": plan_config.base_session_evals,
                "monitoring_allowed": plan_config.monitoring_allowed,
                "max_members": plan_config.max_members,
                "pay_as_you_go": plan_config.pay_as_you_go,
            },
        )

    async def require_monitoring_allowed(self, org_id: UUID) -> None:
        """Raise ``QuotaExceededError`` if the plan does not permit monitors."""
        sub = await self._repo.get_subscription_by_org(org_id)
        if sub is None:
            raise QuotaExceededError("No active subscription found for this organization.")
        plan_config = get_plan_config(SubscriptionPlan(sub.plan))
        if not plan_config.monitoring_allowed:
            raise QuotaExceededError(f"Monitoring is not available on your {sub.plan} plan. Please upgrade.")
