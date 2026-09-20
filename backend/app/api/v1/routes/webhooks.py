"""Stripe webhook handler with PostgreSQL-backed idempotency."""

import stripe
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.infrastructure.db.engine import async_session_factory
from app.infrastructure.db.repositories.stripe_webhook_repo import StripeWebhookRepository
from app.logging import logger
from app.registry.settings import settings
from app.services.billing_service import BillingService

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

_HANDLED_EVENTS = {
    "checkout.session.completed",
    "invoice.created",
    "invoice.paid",
    "invoice.payment_failed",
    "customer.subscription.updated",
    "customer.subscription.deleted",
}


@router.post("/stripe")
async def stripe_webhook(request: Request) -> JSONResponse:
    """Verify, claim, and process a Stripe webhook event."""
    payload = await request.body()
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload,
            sig_header,
            settings.STRIPE_WEBHOOK_SECRET,
        )
    except ValueError:
        logger.warning("stripe_webhook_invalid_payload")
        return JSONResponse(status_code=400, content={"detail": "Invalid payload"})
    except stripe.SignatureVerificationError:
        logger.warning("stripe_webhook_invalid_signature")
        return JSONResponse(status_code=400, content={"detail": "Invalid signature"})

    event_type = event["type"]
    event_id = event["id"]
    event_data = event["data"]
    if event_type not in _HANDLED_EVENTS:
        return JSONResponse(status_code=200, content={"received": True})

    async with async_session_factory() as session:
        webhook_repo = StripeWebhookRepository(session)
        acquired = await webhook_repo.try_claim(event_id, event_type)
        await session.commit()
        if not acquired:
            logger.info("stripe_webhook_duplicate", event_id=event_id, event_type=event_type)
            return JSONResponse(status_code=200, content={"received": True})

        try:
            billing_svc = BillingService(session)
            match event_type:
                case "checkout.session.completed":
                    await billing_svc.handle_checkout_completed(event_data)
                case "invoice.created":
                    await billing_svc.handle_invoice_created(event_data)
                case "invoice.paid":
                    await billing_svc.handle_invoice_paid(event_data)
                case "invoice.payment_failed":
                    await billing_svc.handle_invoice_payment_failed(event_data)
                case "customer.subscription.updated":
                    await billing_svc.handle_subscription_updated(event_data)
                case "customer.subscription.deleted":
                    await billing_svc.handle_subscription_deleted(event_data)

            await webhook_repo.mark_completed(event_id)
            await session.commit()
        except Exception as exc:
            await session.rollback()
            await webhook_repo.mark_failed(event_id, str(exc))
            await session.commit()
            logger.exception("stripe_webhook_failed", event_type=event_type, event_id=event_id)
            raise

    logger.info("stripe_webhook_processed", event_type=event_type, event_id=event_id)
    return JSONResponse(status_code=200, content={"received": True})
