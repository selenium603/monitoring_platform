"""Repository for durable Stripe webhook idempotency."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.models import StripeWebhookEventModel
from app.registry.constants import WebhookEventStatus


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StripeWebhookRepository:
    """Claim and update webhook events without committing transactions."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def try_claim(self, event_id: str, event_type: str) -> bool:
        """Claim a new, failed, or stale processing event."""
        now = _utcnow()
        statement = (
            insert(StripeWebhookEventModel)
            .values(
                event_id=event_id,
                event_type=event_type,
                status=WebhookEventStatus.PROCESSING.value,
                locked_at=now,
                completed_at=None,
                last_error=None,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["event_id"])
            .returning(StripeWebhookEventModel.event_id)
        )
        result = await self._session.execute(statement)
        if result.scalar_one_or_none() is not None:
            return True

        stale_before = now - timedelta(minutes=5)
        reclaim = (
            update(StripeWebhookEventModel)
            .where(
                StripeWebhookEventModel.event_id == event_id,
                or_(
                    StripeWebhookEventModel.status == WebhookEventStatus.FAILED.value,
                    and_(
                        StripeWebhookEventModel.status == WebhookEventStatus.PROCESSING.value,
                        StripeWebhookEventModel.locked_at <= stale_before,
                    ),
                ),
            )
            .values(
                status=WebhookEventStatus.PROCESSING.value,
                event_type=event_type,
                locked_at=now,
                completed_at=None,
                last_error=None,
                updated_at=now,
            )
            .returning(StripeWebhookEventModel.event_id)
        )
        result = await self._session.execute(reclaim)
        return result.scalar_one_or_none() is not None

    async def mark_completed(self, event_id: str) -> None:
        """Mark an event as successfully handled."""
        now = _utcnow()
        await self._session.execute(
            update(StripeWebhookEventModel)
            .where(StripeWebhookEventModel.event_id == event_id)
            .values(
                status=WebhookEventStatus.COMPLETED.value,
                completed_at=now,
                last_error=None,
                updated_at=now,
            )
        )

    async def mark_failed(self, event_id: str, error: str) -> None:
        """Record a failed attempt so a later delivery can reclaim it."""
        await self._session.execute(
            update(StripeWebhookEventModel)
            .where(StripeWebhookEventModel.event_id == event_id)
            .values(
                status=WebhookEventStatus.FAILED.value,
                last_error=error[:4000],
                updated_at=_utcnow(),
            )
        )
