from decimal import Decimal
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, update

from app.core.billing.plans import OVERAGE_UNIT_PRICE
from app.infrastructure.db.models import SubscriptionModel, UsageRecordModel
from app.infrastructure.db.repositories.billing_repo import BillingRepository
from app.registry.constants import SubscriptionPlan, SubscriptionStatus, UsageCategory
from app.registry.exceptions import QuotaExceededError
from app.services.billing_service import BillingService
from app.services.usage_service import UsageService

from .conftest import TEST_ORG_ID
from .factories import _serialize_payload, build_trace_payload


@pytest.fixture(autouse=True)
async def _clear_seeded_subscription_for_usage_tests(db_session):
    await db_session.execute(delete(UsageRecordModel).where(UsageRecordModel.org_id == TEST_ORG_ID))
    await db_session.execute(delete(SubscriptionModel).where(SubscriptionModel.org_id == TEST_ORG_ID))
    await db_session.commit()
    yield


async def _set_usage(db_session, org_id, period_start, **values):
    await db_session.execute(
        update(UsageRecordModel)
        .where(
            UsageRecordModel.org_id == org_id,
            UsageRecordModel.period_start == period_start,
        )
        .values(**values)
    )


async def _create_subscription(db_session, *, plan=SubscriptionPlan.HOBBY, **kwargs):
    repo = BillingRepository(db_session)
    sub = await repo.create_subscription(TEST_ORG_ID, plan=plan, **kwargs)
    await repo.get_or_create_usage_record(TEST_ORG_ID, sub.current_period_start, sub.current_period_end)
    await db_session.commit()
    return sub


async def test_check_and_increment_increments_for_paid_plan(db_session):
    sub = await _create_subscription(
        db_session,
        plan=SubscriptionPlan.PRO,
        stripe_subscription_id="sub_paid",
    )

    n = await UsageService(db_session).check_and_increment(TEST_ORG_ID, UsageCategory.TRACES)
    await db_session.commit()

    assert n == 1
    usage = await BillingRepository(db_session).get_current_usage_record(TEST_ORG_ID, sub.current_period_start)
    assert usage is not None
    assert usage.trace_count == 1


async def test_get_current_usage_reads_postgres_counters(db_session):
    sub = await _create_subscription(
        db_session,
        plan=SubscriptionPlan.PRO,
        stripe_subscription_id="sub_usage",
    )
    await _set_usage(
        db_session,
        TEST_ORG_ID,
        sub.current_period_start,
        trace_count=4,
        trace_eval_count=1,
        session_eval_count=2,
    )
    await db_session.commit()

    summary = await UsageService(db_session).get_current_usage(TEST_ORG_ID)
    assert summary.traces == 4
    assert summary.trace_evals == 1
    assert summary.session_evals == 2


async def test_hobby_trace_at_99_allows_one_more(db_session):
    sub = await _create_subscription(db_session)
    await _set_usage(db_session, TEST_ORG_ID, sub.current_period_start, trace_count=99)
    await db_session.commit()

    n = await UsageService(db_session).check_and_increment(TEST_ORG_ID, UsageCategory.TRACES)
    await db_session.commit()
    assert n == 100


async def test_hobby_trace_at_100_blocks_next(db_session):
    sub = await _create_subscription(db_session)
    await _set_usage(db_session, TEST_ORG_ID, sub.current_period_start, trace_count=100)
    await db_session.commit()

    with pytest.raises(QuotaExceededError):
        await UsageService(db_session).check_and_increment(TEST_ORG_ID, UsageCategory.TRACES)
    await db_session.rollback()

    usage = await BillingRepository(db_session).get_current_usage_record(TEST_ORG_ID, sub.current_period_start)
    assert usage is not None
    assert usage.trace_count == 100


async def test_hobby_bulk_increment_crossing_limit_is_rejected_atomically(db_session):
    sub = await _create_subscription(db_session)
    await _set_usage(db_session, TEST_ORG_ID, sub.current_period_start, trace_eval_count=98)
    await db_session.commit()

    with pytest.raises(QuotaExceededError):
        await UsageService(db_session).check_and_increment(TEST_ORG_ID, UsageCategory.TRACE_EVALS, count=5)
    await db_session.rollback()

    usage = await BillingRepository(db_session).get_current_usage_record(TEST_ORG_ID, sub.current_period_start)
    assert usage is not None
    assert usage.trace_eval_count == 98


async def test_hobby_session_eval_limit_enforced(db_session):
    sub = await _create_subscription(db_session)
    await _set_usage(db_session, TEST_ORG_ID, sub.current_period_start, session_eval_count=10)
    await db_session.commit()

    with pytest.raises(QuotaExceededError):
        await UsageService(db_session).check_and_increment(TEST_ORG_ID, UsageCategory.SESSION_EVALS)
    await db_session.rollback()


async def test_paid_plan_above_base_limit_allows_increment(db_session):
    sub = await _create_subscription(
        db_session,
        plan=SubscriptionPlan.PRO,
        stripe_subscription_id="sub_payg",
    )
    await _set_usage(db_session, TEST_ORG_ID, sub.current_period_start, trace_eval_count=999999)
    await db_session.commit()

    n = await UsageService(db_session).check_and_increment(
        TEST_ORG_ID,
        UsageCategory.TRACE_EVALS,
        count=100,
    )
    await db_session.commit()
    assert n == 1_000_099


async def test_hobby_bulk_fitting_exactly_at_limit_succeeds(db_session):
    sub = await _create_subscription(db_session)
    await _set_usage(db_session, TEST_ORG_ID, sub.current_period_start, trace_eval_count=95)
    await db_session.commit()

    n = await UsageService(db_session).check_and_increment(
        TEST_ORG_ID,
        UsageCategory.TRACE_EVALS,
        count=5,
    )
    await db_session.commit()
    assert n == 100


async def test_rollback_increment_decrements_postgres_counter(db_session):
    sub = await _create_subscription(
        db_session,
        plan=SubscriptionPlan.PRO,
        stripe_subscription_id="sub_rollback",
    )
    svc = UsageService(db_session)
    await svc.check_and_increment(TEST_ORG_ID, UsageCategory.TRACE_EVALS, count=3_000)
    await svc.rollback_increment(TEST_ORG_ID, UsageCategory.TRACE_EVALS, count=3_000)
    await db_session.commit()

    usage = await BillingRepository(db_session).get_current_usage_record(TEST_ORG_ID, sub.current_period_start)
    assert usage is not None
    assert usage.trace_eval_count == 0


async def test_no_subscription_raises_quota_error(db_session):
    with pytest.raises(QuotaExceededError, match="No active subscription"):
        await UsageService(db_session).check_and_increment(TEST_ORG_ID, UsageCategory.TRACES)


async def test_canceled_subscription_raises_quota_error(db_session):
    await _create_subscription(db_session)
    await BillingRepository(db_session).update_subscription(
        TEST_ORG_ID,
        status=SubscriptionStatus.CANCELED.value,
    )
    await db_session.commit()

    with pytest.raises(QuotaExceededError, match="not active"):
        await UsageService(db_session).check_and_increment(TEST_ORG_ID, UsageCategory.TRACES)


async def test_post_traces_succeeds_when_under_quota(client: AsyncClient, db_session):
    await _create_subscription(db_session)
    payload = _serialize_payload(build_trace_payload(name="under-quota"))
    resp = await client.post("/traces", json=payload)
    assert resp.status_code == 202


async def test_post_traces_returns_429_when_hobby_trace_limit_reached(client: AsyncClient, db_session):
    sub = await _create_subscription(db_session)
    await _set_usage(db_session, TEST_ORG_ID, sub.current_period_start, trace_count=100)
    await db_session.commit()

    payload = _serialize_payload(build_trace_payload(name="over-quota"))
    resp = await client.post("/traces", json=payload)
    assert resp.status_code == 429


# ---------------------------------------------------------------------------
# Overage billing: PostgreSQL counters, delta calculation, and idempotency
# ---------------------------------------------------------------------------


async def test_unreported_overages_computes_correct_delta(db_session):
    sub = await _create_subscription(
        db_session,
        plan=SubscriptionPlan.PRO,
        stripe_subscription_id="sub_delta",
    )
    await _set_usage(
        db_session,
        TEST_ORG_ID,
        sub.current_period_start,
        trace_count=5100,
        trace_eval_count=5000,
        session_eval_count=50,
    )
    await db_session.commit()

    overages = await BillingService(db_session).calculate_unreported_overages(TEST_ORG_ID)
    assert overages.trace_overage == 100
    assert overages.trace_eval_overage == 0
    assert overages.session_eval_overage == 0
    assert overages.total_cost == OVERAGE_UNIT_PRICE * 100


async def test_unreported_overages_second_call_yields_only_new_delta(db_session):
    sub = await _create_subscription(
        db_session,
        plan=SubscriptionPlan.PRO,
        stripe_subscription_id="sub_delta2",
    )
    await _set_usage(
        db_session,
        TEST_ORG_ID,
        sub.current_period_start,
        trace_count=5100,
        trace_eval_count=5000,
        session_eval_count=50,
        reported_trace_count=5100,
        reported_trace_eval_count=5000,
        reported_session_eval_count=50,
    )
    await db_session.commit()

    overages = await BillingService(db_session).calculate_unreported_overages(TEST_ORG_ID)
    assert overages.total_cost == Decimal("0")
    assert overages.trace_overage == 0


async def test_unreported_overages_incremental_growth(db_session):
    sub = await _create_subscription(
        db_session,
        plan=SubscriptionPlan.PRO,
        stripe_subscription_id="sub_inc",
    )
    await _set_usage(
        db_session,
        TEST_ORG_ID,
        sub.current_period_start,
        trace_count=5150,
        trace_eval_count=5000,
        session_eval_count=50,
        reported_trace_count=5100,
        reported_trace_eval_count=5000,
        reported_session_eval_count=50,
    )
    await db_session.commit()

    overages = await BillingService(db_session).calculate_unreported_overages(TEST_ORG_ID)
    assert overages.trace_overage == 50
    assert overages.total_cost == OVERAGE_UNIT_PRICE * 50


async def test_unreported_overages_below_base_returns_zero(db_session):
    sub = await _create_subscription(
        db_session,
        plan=SubscriptionPlan.PRO,
        stripe_subscription_id="sub_below",
    )
    await _set_usage(
        db_session,
        TEST_ORG_ID,
        sub.current_period_start,
        trace_count=4000,
        trace_eval_count=3000,
        session_eval_count=50,
    )
    await db_session.commit()

    overages = await BillingService(db_session).calculate_unreported_overages(TEST_ORG_ID)
    assert overages.total_cost == Decimal("0")


async def test_unreported_overages_cross_base_transition(db_session):
    sub = await _create_subscription(
        db_session,
        plan=SubscriptionPlan.PRO,
        stripe_subscription_id="sub_cross",
    )
    await _set_usage(
        db_session,
        TEST_ORG_ID,
        sub.current_period_start,
        trace_count=5100,
        trace_eval_count=4000,
        session_eval_count=50,
        reported_trace_count=4800,
        reported_trace_eval_count=4000,
        reported_session_eval_count=50,
    )
    await db_session.commit()

    overages = await BillingService(db_session).calculate_unreported_overages(TEST_ORG_ID)
    assert overages.trace_overage == 100


async def test_unreported_overages_billed_period_returns_zero(db_session):
    sub = await _create_subscription(
        db_session,
        plan=SubscriptionPlan.PRO,
        stripe_subscription_id="sub_billed",
    )
    await _set_usage(
        db_session,
        TEST_ORG_ID,
        sub.current_period_start,
        trace_count=10000,
        trace_eval_count=10000,
        session_eval_count=500,
        billed=True,
    )
    await db_session.commit()

    overages = await BillingService(db_session).calculate_unreported_overages(TEST_ORG_ID)
    assert overages.total_cost == Decimal("0")


async def test_unreported_overages_hobby_returns_zero(db_session):
    sub = await _create_subscription(db_session)
    await _set_usage(
        db_session,
        TEST_ORG_ID,
        sub.current_period_start,
        trace_count=200,
        trace_eval_count=200,
        session_eval_count=20,
    )
    await db_session.commit()

    overages = await BillingService(db_session).calculate_unreported_overages(TEST_ORG_ID)
    assert overages.total_cost == Decimal("0")


async def test_report_overages_advances_watermark(db_session):
    sub = await _create_subscription(
        db_session,
        plan=SubscriptionPlan.PRO,
        stripe_customer_id="cus_test",
        stripe_subscription_id="sub_wm",
    )
    await _set_usage(
        db_session,
        TEST_ORG_ID,
        sub.current_period_start,
        trace_count=5200,
        trace_eval_count=5000,
        session_eval_count=50,
    )
    await db_session.commit()

    billing_svc = BillingService(db_session)
    with patch("stripe.InvoiceItem.create"):
        result = await billing_svc.report_overages_to_stripe(TEST_ORG_ID)
    await db_session.commit()

    assert result is True
    usage = await BillingRepository(db_session).get_current_usage_record(TEST_ORG_ID, sub.current_period_start)
    assert usage is not None
    assert usage.reported_trace_count == 5200
    assert usage.reported_trace_eval_count == 5000
    assert usage.reported_session_eval_count == 50


async def test_report_overages_idempotent_on_second_call(db_session):
    sub = await _create_subscription(
        db_session,
        plan=SubscriptionPlan.PRO,
        stripe_customer_id="cus_test2",
        stripe_subscription_id="sub_idem",
    )
    await _set_usage(
        db_session,
        TEST_ORG_ID,
        sub.current_period_start,
        trace_count=5200,
        trace_eval_count=5000,
        session_eval_count=50,
    )
    await db_session.commit()

    billing_svc = BillingService(db_session)
    with patch("stripe.InvoiceItem.create") as mock_create:
        assert await billing_svc.report_overages_to_stripe(TEST_ORG_ID) is True
        await db_session.commit()
        assert await billing_svc.report_overages_to_stripe(TEST_ORG_ID) is False
        await db_session.commit()

    assert mock_create.call_count == 1


async def test_overage_lock_uses_postgres_subscription_row(db_session):
    billing_svc = BillingService(db_session)
    assert await billing_svc.try_acquire_overage_lock(TEST_ORG_ID) is False

    await _create_subscription(
        db_session,
        plan=SubscriptionPlan.PRO,
        stripe_subscription_id="sub_lock",
    )
    assert await billing_svc.try_acquire_overage_lock(TEST_ORG_ID) is True
    await db_session.rollback()
